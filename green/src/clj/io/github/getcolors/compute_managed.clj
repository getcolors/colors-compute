(ns io.github.getcolors.compute-managed
  "Managed Kubernetes planning, inspection and journal-guarded lifecycle."
  (:require [cheshire.core :as json] [clojure.java.io :as io] [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-managed-journal :as managed-journal]
            [io.github.getcolors.compute-managed-access :as access]))
(def managed-kubeconfig-path access/managed-kubeconfig-path)
(def ^:private recipes (json/parse-string (slurp (io/resource "colors_compute/managed-providers.json")) true))
(defn- safe? [v] (and (string? v) (boolean (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" v))))
(defn- missing? [v] (or (nil? v) (and (string? v) (or (str/blank? v) (= "REPLACE_ME" (str/upper-case v))))))
(defn- require-valid [condition message] (when-not condition (throw (ex-info message {}))))
(defn- resolve-request [opts request]
  (require-valid (and (map? request) (every? #{:legacy_state_keys} (keys request)) (safe? (:profile opts))) "invalid managed Kubernetes request")
  (let [provider (:provider-compute opts) recipe (when (string? provider) (get recipes (keyword provider)))
        _ (require-valid recipe "managed Kubernetes provider unavailable")
        configured-name (get opts (keyword (str provider "-name"))) name (if (missing? configured-name) (:profile opts) configured-name)
        _ (require-valid (safe? name) "invalid managed Kubernetes name")
        values (into {} (map (fn [[field option]] [field (get opts (keyword option))]) (:options recipe)))
        _ (require-valid (every? #(and (string? (get values %)) (not (missing? (get values %)))
                                      (not (str/includes? (get values %) "${")) (not (str/includes? (get values %) "%{"))) [:region :version :size]) "missing managed Kubernetes settings")
        _ (require-valid (boolean (re-matches (re-pattern (:version_pattern recipe)) (:version values))) "invalid managed Kubernetes version")
        count (:count values) _ (require-valid (and (number? count) (<= 1 count 1000) (== count (Math/floor (double count)))) "invalid managed Kubernetes node count")
        protect (get opts :compute-prevent-destroy true) _ (require-valid (boolean? protect) "invalid compute protection")
        legacy (get request :legacy_state_keys [])
        _ (require-valid (and (vector? legacy) (= (clojure.core/count legacy) (clojure.core/count (set legacy)))
                              (every? #(and (string? %) (re-matches (re-pattern (str (java.util.regex.Pattern/quote (:profile opts)) "/[A-Za-z0-9][A-Za-z0-9_-]{0,62}\\.tfstate")) %)) legacy)) "invalid legacy compute state keys")
        _ (managed-kubeconfig-path opts) key (str (:profile opts) "/compute/managed-kubernetes.tfstate")]
    (compute/backend-plan opts key)
    {:provider provider :inputs (assoc values :count (long count) :name name :prevent_destroy protect) :state-key key}))
(defn managed-errors
  ([opts] (managed-errors opts {}))
  ([opts request] (try (resolve-request opts request) [] (catch Exception error [(or (ex-message error) "invalid managed Kubernetes request")]))))
(defn plan-managed-kubernetes
  ([opts] (plan-managed-kubernetes opts {}))
  ([opts request]
   (let [{:keys [provider inputs state-key]} (resolve-request opts request)]
     {:status "planned" :params (merge {:provider provider :kind "managed-kubernetes" :name (:name inputs) :cluster_id "planned-cluster" :endpoint "https://192.0.2.10"}
                                       (get-in recipes [(keyword provider) :planning_params]))
      :state_key state-key :documents (assoc (compute/provider-plan provider "managed-kubernetes" inputs) "backend.tf.json" (:config (compute/backend-plan opts state-key)))})))
(defn managed-application-settings
  "Provider details consumed by application Kubernetes manifests."
  ([opts] (managed-application-settings opts nil))
  ([opts params]
   (let [{:keys [provider]} (resolve-request opts {})
         observed (if (nil? params) (:params (plan-managed-kubernetes opts)) (access/public-params params provider))
         traits (get-in recipes [(keyword provider) :traits])
         pod-cidr (if (= "observed" (:pod_cidr_source traits)) (:pod_cidr observed) (get opts (keyword (:pod_cidr_option traits))))]
     (when (some? pod-cidr) (access/public-params (assoc observed :pod_cidr pod-cidr) provider))
     (cond-> {:load_balancer_annotations (into {} (map (fn [[key value]] [key (str/replace value "{{name}}" (:name observed))]) (:load_balancer_annotations traits)))
              :storage_class (:storage_class traits)}
       (some? pod-cidr) (assoc :pod_cidr pod-cidr)))))

(defn managed-application-artifacts
  "Library-owned scripts for the selected managed provider."
  ([opts] (managed-application-artifacts opts []))
  ([opts required]
   (let [{:keys [provider]} (resolve-request opts {})
         artifacts (get (json/parse-string (slurp (io/resource "colors_compute/managed-artifacts.json"))) provider)]
     (require-valid (and (vector? required) (every? #(and (string? %) (contains? artifacts %)) required)) "managed provider lacks required application artifact")
     artifacts)))

(defn ^:no-doc read-managed-state
  ([opts key environment] (read-managed-state opts key environment false runtime/run-command))
  ([opts key environment write] (read-managed-state opts key environment write runtime/run-command))
  ([opts key environment write runner]
   (let [decoder (access/access-decoder opts environment) result (runtime/read-state opts key environment runner true (:decode decoder))]
     (cond
       (not= "present" (:status result)) {:status "error"}
       (:state_empty result) result
       :else (cond-> {:status "present" :params (access/public-params (:params result) (:provider-compute opts))}
               write (assoc :kubeconfig_path ((:write decoder))))))))
(defn- identity [opts]
  (let [plan (compute/backend-plan opts (str (:profile opts) "/compute/managed-kubernetes.tfstate"))
        backend (get-in plan [:config :terraform :backend (keyword (:provider-backend opts))])]
    ;; OpenTofu uses the s3 backend type for both provider choices.
    (let [backend (or backend (get-in plan [:config :terraform :backend :s3]))]
      {:profile (:profile opts) :provider (:provider-compute opts)
       :backend (cond-> {:kind (:provider-backend opts) :bucket (:bucket backend) :region (:region backend)}
                  (= "r2" (:provider-backend opts)) (assoc :endpoint (get-in backend [:endpoints :s3])))})))
(defn- make-coordinator
  ([opts env] (make-coordinator opts env nil))
  ([opts env read] (coordinator/coordinator opts env read nil nil {:event-prefix "managed/" :reducer managed-journal/managed-coordination})))
(defn read-managed-kubernetes
  ([opts] (read-managed-kubernetes opts {}))
  ([opts request] (read-managed-kubernetes opts request (into {} (System/getenv)) {}))
  ([opts request environment] (read-managed-kubernetes opts request environment {}))
  ([opts request environment dependencies]
   (let [owner (atom nil) acquired (atom false) cancelled (atom nil) result (atom {:status "error"})]
     (try
       (let [{:keys [state-key]} (resolve-request opts request)
             read-object #((get dependencies :journal-get journal/journal-get) opts environment)
             observed (read-object) document (:document observed)]
         (reset! result
           (cond
             (= {:status "absent"} observed) observed
             (not (and (= "present" (:status observed)) (managed-journal/managed-document-valid? document)
                       (= (:identity document) (identity opts)) (= "idle" (get-in document [:lock :state])))) {:status "error"}
             (= "retired" (:status document)) {:status "destroyed"}
             (not (contains? #{"ready" "failed"} (get-in document [:shared :phase]))) {:status "error"}
             :else
             (do
               (reset! owner ((get dependencies :coordinator make-coordinator) opts environment
                               #(let [value (read-object)] (require-valid (= "present" (:status value)) "managed journal unavailable") value)))
               (coordinator/acquire! @owner) (reset! acquired true)
               (let [doc (:document (coordinator/snapshot @owner))]
                 (cond
                   (= "retired" (:status doc)) {:status "destroyed"}
                   (not (contains? #{"ready" "failed"} (get-in doc [:shared :phase]))) {:status "error"}
                   :else ((get dependencies :read-managed-state read-managed-state) opts state-key environment true)))))))
       (catch InterruptedException error (reset! cancelled error))
       (catch Exception _ (reset! result {:status "error"})))
     (when @acquired
       (try (coordinator/release! @owner)
            (catch InterruptedException error (reset! cancelled error))
            (catch Exception _ (reset! result {:status "error"}))))
     (if @cancelled (throw @cancelled) @result))))
(defn managed-version-preflight [opts environment]
  (try
    (let [recipe (get recipes (keyword (:provider-compute opts))) descriptor (:versions recipe)
          token (get environment (:credential descriptor))]
      (if-not (and (string? token) (not (str/blank? token))) false
        (let [connection ^java.net.HttpURLConnection (.openConnection (java.net.URL. (:url descriptor)))]
          (try
            (.setInstanceFollowRedirects connection false) (.setConnectTimeout connection 30000) (.setReadTimeout connection 30000)
            (.setRequestProperty connection "Authorization" (str "Bearer " token)) (.setRequestProperty connection "Accept" "application/json")
            (if-not (= 200 (.getResponseCode connection)) false
              (with-open [stream (.getInputStream connection)]
                (let [raw (.readNBytes stream 2097153)]
                  (if (> (alength raw) 2097152) false
                    (let [response (journal/parse-one (String. raw java.nio.charset.StandardCharsets/UTF_8))
                          values (get-in response (mapv keyword (:path descriptor)))
                          versions (if (:value descriptor) (mapv #(get % (keyword (:value descriptor))) values) values)]
                      (and (vector? values) (seq values) (every? string? versions)
                           (boolean (some #{(get opts (keyword (get-in recipe [:options :version])))} versions))))))))
            (finally (.disconnect connection))))))
    (catch InterruptedException error (throw error))
    (catch Exception _ false)))
(defn managed-kubernetes
  ([opts] (managed-kubernetes opts {}))
  ([opts request] (managed-kubernetes opts request (into {} (System/getenv)) {}))
  ([opts request environment] (managed-kubernetes opts request environment {}))
  ([opts request environment dependencies]
   (let [owner (atom nil) acquired (atom false) cancelled (atom nil) result (atom {:status "error"})]
     (letfn [(call [name default & args] (apply (get dependencies name default) args))
             (require! [condition] (require-valid condition "managed Kubernetes lifecycle refused"))
             (execute []
               (let [errors (managed-errors opts request)]
                 (if (seq errors) {:status "error" :errors errors}
                   (let [plan (plan-managed-kubernetes opts request) operation (name (get opts :green/event :create))]
                     (require! (and (contains? #{"create" "delete"} operation) (not (true? (:green/dry-run opts)))))
                     (require! (or (not= "delete" operation) (false? (:compute-prevent-destroy opts))))
                     (reset! owner (call :coordinator
                       make-coordinator opts environment))
                     (coordinator/acquire! @owner) (reset! acquired true)
                     (doseq [legacy (:legacy_state_keys request)]
                       (require! (= {:status "absent"} (call :state-presence (fn [opts key env legacy] (execution/state-presence opts key env runtime/run-command legacy)) opts legacy environment true))))
                     (let [doc (:document (coordinator/snapshot @owner)) phase (get-in doc [:shared :phase])
                           presence (call :state-presence execution/state-presence opts (:state_key plan) environment)]
                       (require! (contains? #{{:status "present"} {:status "absent"}} presence))
                       (if (contains? #{"declared" "destroyed"} phase)
                         (when (= {:status "present"} presence)
                           (let [empty (call :read-empty (fn [opts key env] (runtime/read-state opts key env runtime/run-command true)) opts (:state_key plan) environment)]
                             (require! (and (= "present" (:status empty)) (true? (:state_empty empty))))))
                         (do (require! (= {:status "present"} presence))
                             (let [observed (call :read-managed-state read-managed-state opts (:state_key plan) environment)]
                               (require! (and (= "present" (:status observed)) (= (:provider-compute opts) (get-in observed [:params :provider])))))))
                       (when (= "retired" (:status doc))
                         (if (= operation "delete") (reset! result {:status "destroyed"}) (coordinator/transition! @owner "recreate")))
                       (if (= "destroyed" (:status @result)) @result
                         (do
                           (when (and (= phase "failed") (= operation "create"))
                             (require! (= "create" (get-in doc [:shared :operation])))
                             (coordinator/transition! @owner "shared-retry" {:evidence "readable-state"}))
                           (let [errors (compute/compute-credential-errors opts environment)]
                             (if (and (seq errors) (not (and (= operation "delete") (= presence {:status "absent"}))))
                               {:status "error" :errors errors}
                               (let [_ (when (= operation "create") (require! (true? (call :version-preflight managed-version-preflight opts environment))))
                                     attempt (coordinator/transition! @owner (if (= operation "create") "shared-start" "shared-destroy"))
                                     decoder (access/access-decoder opts environment)
                                     outcome (call :converge-state
                                       (fn [opts key documents operation presence env decoder]
                                         (execution/converge-state opts key documents operation presence env runtime/run-command #(Thread/sleep %) (:decode decoder)))
                                       opts (:state_key plan) (dissoc (:documents plan) "backend.tf.json") operation presence environment decoder)]
                                 (if (= (:status outcome) (if (= operation "create") "ready" "destroyed"))
                                   (do (coordinator/transition! @owner "shared-complete" {:operation_id attempt})
                                       (cond-> {:status (:status outcome)} (= operation "create")
                                         (assoc :params (access/public-params (:params outcome) (:provider-compute opts)) :kubeconfig_path ((:write decoder)))))
                                   (do (coordinator/transition! @owner "shared-fail" {:operation_id attempt}) {:status "error"}))))))))))))]
       (try (reset! result (execute))
            (catch InterruptedException error (reset! cancelled error))
            (catch Exception _ (reset! result {:status "error"})))
       (when @acquired
         (try (coordinator/release! @owner)
              (catch InterruptedException error (reset! cancelled error))
              (catch Exception _ (reset! result {:status "error"}))))
       (if @cancelled (throw @cancelled) @result)))))
