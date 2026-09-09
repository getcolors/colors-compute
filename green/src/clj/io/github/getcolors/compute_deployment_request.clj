(ns io.github.getcolors.compute-deployment-request
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-controller :as controller]))
(def recipes (json/parse-string (slurp (io/resource "colors_compute/provider-recipes.json")) true))
(defn- fail [message] (throw (ex-info message {})))
(defn- safe? [value] (and (string? value) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value))))
(defn- missing? [value] (or (nil? value) (and (string? value) (or (str/blank? value) (= "REPLACE_ME" (str/upper-case (str/trim value)))))))
(defn deployment-requests [opts topology requirements key]
  (let [provider (:provider-compute opts) recipe (when (string? provider) (get recipes (keyword provider)))]
    (when-not recipe (fail "compute provider recipe unavailable"))
    (when-not (and (map? requirements) (contains? requirements :security) (every? #{:security :network :single_host :legacy_state_keys :private :endpoint :roles :entry_node_id :kubernetes_controller} (keys requirements)))
      (fail "invalid deployment requirements"))
    (when (contains? requirements :kubernetes_controller)
      (when-not (true? (:kubernetes_controller requirements)) (fail "invalid Kubernetes controller requirement"))
      (controller/controller-artifact opts (:planning_shared recipe)))
    (let [single (get requirements :single_host false) nodes (compute/expand topology)]
      (when-not (boolean? single) (fail "invalid single-host requirement"))
      (when (or (> (count nodes) 1000) (and single (or (not= 1 (count nodes)) (some? (:role (first nodes)))))) (fail "invalid deployment topology"))
      (let [role-names (set (keep :role nodes)) roles (:roles requirements)
            settings (get opts :compute-role-settings {}) entry (get requirements :entry_node_id (:node_id (first nodes)))]
        (when (some? roles)
          (when-not (and (map? roles) (= role-names (set (map name (keys roles)))) (every? :role nodes))
            (fail "invalid deployment role policies"))
          (doseq [[_ policy] roles]
            (when-not (and (map? policy) (= #{:security} (set (keys policy))) (map? (:security policy)) (vector? (get-in policy [:security :ingress]))) (fail "invalid deployment role policies"))
            (doseq [rule (get-in policy [:security :ingress])]
              (when-not (map? rule) (fail "invalid deployment role policies"))
              (when (contains? rule :peer_roles)
              (let [peers (:peer_roles rule)]
                (when-not (and (vector? peers) (seq peers) (every? #(and (string? %) (contains? role-names %)) peers) (= (count peers) (count (set peers))))
                  (fail "invalid deployment peer roles")))))))
        (when-not (contains? (set (map :node_id nodes)) entry) (fail "invalid deployment entry node"))
        (when-not (and (map? settings) (every? role-names (map name (keys settings)))) (fail "invalid compute role settings"))
        (doseq [[_ value] settings]
          (when-not (and (map? value) (every? #{:size :image} (keys value)) (not-any? missing? (vals value)))
            (fail "invalid compute role settings"))))
      (when-not (safe? (:profile opts)) (fail ":profile must be a safe identifier"))
      (let [override (get opts (keyword (str provider "-name"))) name (if (missing? override) (:profile opts) override)
            network (get requirements :network {})]
        (when-not (safe? name) (fail "invalid compute name"))
        (when-not (map? network) (fail "invalid compute network request"))
        (let [base (cond-> {:key (select-keys key [:mode :public_key :ids :reference])
                    :network (merge {:mode (:network_mode recipe)} network) :security (:security requirements)} (contains? requirements :endpoint) (assoc :endpoint (:endpoint requirements)))
              requests (mapv (fn [node]
                               (let [node-name (if single name (str name "-" (:node_id node)))]
                                 (when-not (safe? node-name) (fail "invalid derived compute name"))
                                 (cond-> (assoc base :node_id (:node_id node) :name node-name)
                                   (:role node) (assoc :role (:role node))
                                   (some? (:roles requirements)) (assoc :roles (:roles requirements) :security (get-in requirements [:roles (keyword (:role node)) :security]))))) nodes)]
          {:shared (cond-> (assoc base :node_id "shared" :name name) (some? (:roles requirements)) (assoc :roles (:roles requirements)))
           :nodes requests :entry_node_id (get requirements :entry_node_id (:node_id (first nodes)))})))))

(defn source-cidrs
  ([opts suffix] (source-cidrs opts suffix nil))
  ([opts suffix application-key]
   (when-not (and (string? suffix) (re-matches #"[a-z][a-z0-9-]*" suffix)) (fail "invalid compute source setting"))
   (let [application-key (when application-key (keyword application-key))
         value (if (and application-key (contains? opts application-key)) (get opts application-key)
                   (get opts (keyword (str (:provider-compute opts) "-" suffix))))]
     (cond (nil? value) []
           (string? value) (vec (remove empty? (str/split (str/trim value) #"[\s,]+")))
           (and (vector? value) (every? string? value)) value
           :else (fail "invalid compute source list")))))
