(ns io.github.getcolors.compute-deployment-request
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute]))
(def recipes (json/parse-string (slurp (io/resource "colors_compute/provider-recipes.json")) true))
(defn- fail [message] (throw (ex-info message {})))
(defn- safe? [value] (and (string? value) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value))))
(defn- missing? [value] (or (nil? value) (and (string? value) (or (str/blank? value) (= "REPLACE_ME" (str/upper-case (str/trim value)))))))
(defn deployment-requests [opts topology requirements key]
  (let [provider (:provider-compute opts) recipe (when (string? provider) (get recipes (keyword provider)))]
    (when-not recipe (fail "compute provider recipe unavailable"))
    (when-not (and (map? requirements) (contains? requirements :security) (every? #{:security :network :single_host :legacy_state_keys :private :endpoint} (keys requirements)))
      (fail "invalid deployment requirements"))
    (let [single (get requirements :single_host false) nodes (compute/expand topology)]
      (when-not (boolean? single) (fail "invalid single-host requirement"))
      (when (or (> (count nodes) 1000) (and single (or (not= 1 (count nodes)) (some? (:role (first nodes)))))) (fail "invalid deployment topology"))
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
                                 (assoc base :node_id (:node_id node) :name node-name))) nodes)]
          {:shared (assoc base :node_id "shared" :name name) :nodes requests})))))

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
