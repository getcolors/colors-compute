(ns io.github.getcolors.compute
  "Pure version-one compute contract. No cloud or filesystem mutation."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]))

(def registry (json/parse-string (slurp (io/resource "colors_compute/providers.json")) true))
(defn- fail [message] (throw (ex-info message {})))
(defn- label [x] (if (nil? x) "null" (str x)))
(defn- safe? [x] (and (string? x) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" x))))
(defn- missing? [x] (or (nil? x) (and (string? x) (or (str/blank? x) (= "replace_me" (str/lower-case (str/trim x)))))))
(defn- entry [kind selection] (when (string? selection) (get-in registry [kind (keyword selection)])))
(defn- selection-errors [opts]
  (cond-> []
    (nil? (entry :compute (:provider-compute opts)))
    (conj (str ":provider-compute must be one of " (str/join ", " (sort (map name (keys (:compute registry)))))))
    (nil? (entry :backend (:provider-backend opts)))
    (conj ":provider-backend must be one of r2, s3")))

(defn validate [opts]
  (into (cond-> (selection-errors opts)
          (missing? (:profile opts)) (conj ":profile is required")
          (and (not (missing? (:profile opts))) (not (safe? (:profile opts)))) (conj ":profile must be a safe identifier"))
        (concat (for [key (sort (distinct (concat (:required (entry :compute (:provider-compute opts)))
                                         (:required (entry :backend (:provider-backend opts))))))
              :when (missing? (get opts (keyword key)))]
          (str ":" key " is required"))
          (for [selected [(entry :compute (:provider-compute opts)) (entry :backend (:provider-backend opts))]
                alternatives (:required-one-of selected)
                :when (not-any? (fn [group] (every? #(not (missing? (get opts (keyword %)))) group)) alternatives)]
            (str "one of " (str/join " or " (map #(str/join " and " (map (fn [key] (str ":" key)) %)) alternatives)) " is required")))))

(defn credential-requirements [opts]
  (let [errors (selection-errors opts)]
    (when (seq errors) (fail (str/join "; " errors)))
    (->> (concat (:secrets (entry :compute (:provider-compute opts)))
                 (:secrets (entry :backend (:provider-backend opts))))
         (map #(str "COLORS_PAR_" (str/upper-case (str/replace % "-" "_")))) distinct sort vec)))

(defn compute-credential-errors [opts environment]
  (let [provider (entry :compute (:provider-compute opts))]
    (when-not provider (fail "invalid compute provider"))
    (vec (for [variable (sort (map #(str "COLORS_PAR_" (str/upper-case (str/replace % "-" "_"))) (:secrets provider)))
               :when (or (not (string? (get environment variable))) (missing? (get environment variable)))]
           (str "required credential is not set: " variable)))))

(defn state-keys [profile node-ids]
  (when-not (safe? profile) (fail ":profile must be a safe identifier"))
  {:shared (str profile "/compute/shared.tfstate")
   :nodes (reduce (fn [nodes id]
                    (when-not (safe? id) (fail (str "invalid node_id: " (label id))))
                    (when (contains? nodes id) (fail (str "duplicate node_id: " id)))
                    (assoc nodes id (str profile "/compute/nodes/" id ".tfstate"))) {} node-ids)})

(defn expand [topology]
  (when (empty? topology) (fail "topology must declare at least one role"))
  (:nodes
   (reduce (fn [{:keys [roles nodes]} declaration]
             (let [role (:role declaration) count (get declaration :count 1)]
               (when-not (or (nil? role) (and (string? role) (re-matches #"[a-z][a-z0-9]*(-[a-z0-9]+)*" role)))
                 (fail "invalid role"))
               (when (contains? roles role) (fail "duplicate role"))
               (when (and (nil? role) (> (clojure.core/count topology) 1)) (fail "a null role must be the only role"))
               (when-not (and (integer? count) (pos? count)) (fail "count must be a positive integer"))
               (let [new-nodes (mapv (fn [index] {:node_id (str (when role (str role "-")) index) :role role :index index}) (range count))]
                 (state-keys "validation" (map :node_id new-nodes))
                 {:roles (conj roles role) :nodes (into nodes new-nodes)})))
           {:roles #{} :nodes []} topology)))

(defn collect [requests results entry-node-id]
  (when (empty? requests) (fail "no nodes requested"))
  (let [requested (reduce (fn [ids request]
                            (let [id (:node_id request)]
                              (when (contains? ids id) (fail (str "duplicate requested node: " (label id))))
                              (conj ids id))) #{} requests)]
    (when-not (contains? requested entry-node-id) (fail (str "unknown entry node: " (label entry-node-id))))
    (let [by-id (reduce (fn [nodes result]
                         (let [id (:node_id result)]
                           (when-not (contains? requested id) (fail (str "undeclared node: " (label id))))
                           (when (contains? nodes id) (fail (str "duplicate node: " (label id))))
                           (assoc nodes id result))) {} results)
          provider (:provider (get by-id (:node_id (first requests))))
          nodes (mapv (fn [request]
                        (let [id (:node_id request) result (get by-id id)]
                          (when-not result (fail (str "missing node: " (label id))))
                          (doseq [field (cond-> [:provider :name :ip :user :sudoer] (true? (:private request)) (conj :vpc_ip))]
                            (when-not (and (string? (get result field)) (not (str/blank? (get result field))))
                              (fail (str "incomplete node " (label id) ": " (name field)))))
                          (when-not (and (= (:provider result) provider) (= (:provider result) (or (:provider request) provider))) (fail (str "provider mismatch: " (label id))))
                          (assoc result :role (:role request) :index (:index request)))) requests)]
      {:provider provider :entry_node_id entry-node-id :nodes nodes})))

(defn state-decision [read selected]
  (case (:status read)
    "absent" {:action "create"}
    "error" (fail "could not read compute state; refusing mutation")
    "present" (let [recorded (get-in read [:params :provider])]
                (when (or (not (string? recorded)) (missing? recorded)) (fail "legacy state requires migration"))
                (when-not (= recorded selected)
                  (fail (str "state holds a " recorded " machine; set provider-compute back to " recorded " and delete first")))
                {:action "reuse"})
    (fail "invalid state read status")))

(defn render-template
  "Render whole-value placeholders in a library-owned JSON document."
  [value inputs]
  (cond
    (string? value)
    (if-let [[_ key] (re-matches #"\{\{([a-z_]+)\}\}" value)]
      (let [key (keyword key)]
        (if (contains? inputs key) (get inputs key)
            (fail (str "missing template input: " (name key)))))
      (if (or (str/includes? value "{{") (str/includes? value "}}"))
        (fail "template placeholders must occupy the entire string") value))
    (map? value) (into (empty value) (map (fn [[key item]] [key (render-template item inputs)]) value))
    (sequential? value) (mapv #(render-template % inputs) value)
    :else value))

(def ^:private template-bundle
  (delay (json/parse-string (slurp (io/resource "colors_compute/templates.json")))))

(defn provider-plan
  "Load packaged provider-stage documents and render without cloud operations."
  [provider stage inputs]
  (let [stages (get @template-bundle provider)]
    (when-not stages
      (fail (str "compute provider templates unavailable: " (label provider))))
    (let [documents (get stages stage)]
      (when-not documents
        (fail (str "unsupported compute stage: " (label stage))))
      (into {} (map (fn [[filename document]]
                      [filename (render-template document inputs)]) documents)))))

(defn backend-plan
  "Prepare non-secret backend configuration and credential binding names only."
  [opts state-key]
  (let [selection (:provider-backend opts)
        backend (entry :backend selection)]
    (when-not backend (fail ":provider-backend must be one of r2, s3"))
    (doseq [key (sort (:required backend))]
      (when (missing? (get opts (keyword key))) (fail (str ":" key " is required"))))
    (when-not (and (string? state-key)
                   (not (str/blank? state-key))
                   (every? #(and (not (#{"." ".."} %))
                                 (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_.-]*" %))
                           (str/split state-key #"/" -1)))
      (fail "invalid state key"))
    (let [settings
          (merge {:key state-key :use_lockfile true}
                 (if (= "s3" selection)
                   {:bucket (:s3-bucket opts) :region (:s3-region opts)}
                   {:bucket (:r2-bucket opts) :region "auto"
                    :endpoints {:s3 (:r2-endpoint opts)}
                    :use_path_style false
                    :skip_credentials_validation true
                    :skip_metadata_api_check true
                    :skip_region_validation true
                    :skip_requesting_account_id true
                    :skip_s3_checksum true}))]
      {:config {:terraform {:backend {:s3 settings}}}
       :credential_bindings
       (into {} (map (fn [[key option]]
                       [(str "COLORS_PAR_" (str/upper-case (str/replace (name key) "-" "_"))) option])
                     (:backend-config backend)))
       :environment {}})))
