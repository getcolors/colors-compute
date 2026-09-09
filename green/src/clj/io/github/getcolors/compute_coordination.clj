(ns io.github.getcolors.compute-coordination
  "Pure conditional-write intentions. No IO or dispatch authorization."
  (:require [clojure.string :as str]
            [io.github.getcolors.compute :as compute]))

(def ^:private maximum 9007199254740991)
(defn- fail [message] (throw (ex-info message {})))
(defn- exact? [value fields] (and (map? value) (= (set (keys value)) fields)))
(defn- safe? [value] (and (string? value) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value))))
(defn- nonblank? [value] (and (string? value) (not (str/blank? value))))
(defn- integer-in? [value low high]
  (and (number? value) (<= low value high) (== value (Math/floor (double value)))))
(defn- role? [value]
  (or (nil? value) (and (string? value) (boolean (re-matches #"[a-z][a-z0-9]*(-[a-z0-9]+)*" value)))))

(defn identity? [identity]
  (and (exact? identity #{:profile :provider :backend})
       (safe? (:profile identity))
       (string? (:provider identity))
       (contains? (:compute compute/registry) (keyword (:provider identity)))
       (let [backend (:backend identity) kind (:kind backend)]
         (and (contains? #{"s3" "r2"} kind)
              (exact? backend (if (= "r2" kind) #{:kind :bucket :region :endpoint} #{:kind :bucket :region}))
              (string? (:bucket backend))
              (boolean (re-matches #"[a-z0-9][a-z0-9.-]{0,62}" (:bucket backend)))
              (safe? (:region backend))
              (or (= "s3" kind)
                  (and (= "auto" (:region backend))
                       (string? (:endpoint backend))
                       (boolean (re-matches #"https://[a-zA-Z0-9.-]+(:[0-9]{1,5})?/?" (:endpoint backend)))))))))

(defn topology [declarations]
  (try
    (when (and (vector? declarations) (seq declarations)
               (every? #(and (map? %) (every? #{:role :count} (keys %))
                             (role? (:role %)) (integer-in? (get % :count 1) 1 1000)) declarations)
               (<= (reduce + (map #(get % :count 1) declarations)) 1000))
      (compute/expand (mapv #(update % :count (fn [value] (long (or value 1)))) declarations)))
    (catch Exception _ nil)))

(defn- event? [event]
  (let [base #{:type :run_id :write_id :target_etag}
        extra (case (:type event)
                "acquire" #{} "release" #{} "declare" #{:topology}
                "start" #{:node_id :operation_id} "complete" #{:node_id :operation_id}
                "fail" #{:node_id :operation_id} nil)]
    (and extra (exact? event (into base extra))
         (safe? (:run_id event)) (safe? (:write_id event))
         (or (nil? (:target_etag event)) (nonblank? (:target_etag event)))
         (case (:type event)
           "declare" (boolean (seq (topology (:topology event))))
           ("start" "complete" "fail") (and (safe? (:node_id event)) (safe? (:operation_id event)))
           true))))

(defn- observation? [observation]
  (case (:status observation)
    ("absent" "error") (exact? observation #{:status})
    "present" (and (exact? observation #{:status :etag :document}) (nonblank? (:etag observation)))
    false))

(defn- node-name [key] (cond (string? key) key (keyword? key) (subs (str key) 1)))
(defn- node-record? [identity lock id record]
  (and (safe? id) (exact? record #{:state_key :role :index :phase :operation_id})
       (role? (:role record)) (integer-in? (:index record) 0 999)
       (= id (str (when (:role record) (str (:role record) "-")) (long (:index record))))
       (= (:state_key record) (str (:profile identity) "/compute/nodes/" id ".tfstate"))
       (contains? #{"declared" "running" "ready" "failed"} (:phase record))
       (if (= "declared" (:phase record)) (nil? (:operation_id record)) (safe? (:operation_id record)))
       (or (not= "running" (:phase record)) (= "held" (:state lock)))))

(defn- document? [document]
  (and (exact? document #{:schema_version :identity :revision :write_id :lock :topology_declared :nodes})
       (integer-in? (:schema_version document) 1 1) (identity? (:identity document))
       (integer-in? (:revision document) 1 maximum) (safe? (:write_id document))
       (let [lock (:lock document)]
         (and (exact? lock #{:state :run_id})
              (case (:state lock) "held" (safe? (:run_id lock)) "idle" (nil? (:run_id lock)) false)))
       (boolean? (:topology_declared document))
       (map? (:nodes document)) (<= (count (:nodes document)) 1000)
       (= (:topology_declared document) (boolean (seq (:nodes document))))
       (every? (fn [[id record]] (node-record? (:identity document) (:lock document) (node-name id) record)) (:nodes document))
       (let [records (vals (:nodes document))
             groups (group-by :role records)
             operations (keep :operation_id records)]
         (and (= (count operations) (count (set operations)))
              (or (not (contains? groups nil)) (= 1 (count groups)))
              (every? (fn [[_ nodes]] (= (sort (map #(long (:index %)) nodes)) (range (count nodes)))) groups)))))

(defn- transition [document event]
  (let [type (:type event) lock (:lock document)]
    (if (= "acquire" type)
      (do (when (= "held" (:state lock)) (fail "coordination lock held"))
          (assoc document :lock {:state "held" :run_id (:run_id event)}))
      (do
        (when-not (= "held" (:state lock)) (fail "coordination lock not held"))
        (when-not (= (:run_id event) (:run_id lock)) (fail "coordination owner mismatch"))
        (case type
          "declare"
          (do (when (:topology_declared document) (fail "coordination topology already declared"))
              (assoc document :topology_declared true :nodes
                     (into {} (map (fn [{:keys [node_id role index]}]
                                     [node_id {:state_key (str (get-in document [:identity :profile]) "/compute/nodes/" node_id ".tfstate")
                                               :role role :index index :phase "declared" :operation_id nil}])
                                   (topology (:topology event))))))
          "release"
          (do (when (some #(= "running" (:phase %)) (vals (:nodes document)))
                (fail "coordination operations outstanding"))
              (assoc document :lock {:state "idle" :run_id nil}))
          (let [id (:node_id event)
                nodes (:nodes document)
                key (if (contains? nodes id) id (keyword id))
                record (get nodes key)]
            (when-not (:topology_declared document) (fail "coordination topology not declared"))
            (when-not record (fail "coordination node undeclared"))
            (if (= "start" type)
              (do
                (when-not (= "declared" (:phase record)) (fail "coordination node not startable"))
                (when (some #(= (:operation_id event) (:operation_id %)) (vals nodes))
                  (fail "coordination operation_id reused"))
                (assoc-in document [:nodes key] (assoc record :phase "running" :operation_id (:operation_id event))))
              (do
                (when-not (= "running" (:phase record)) (fail "coordination node not running"))
                (when-not (= (:operation_id event) (:operation_id record)) (fail "coordination operation mismatch"))
                (assoc-in document [:nodes key :phase] (if (= "complete" type) "ready" "failed"))))))))))

(defn schema-one-coordination
  "Plan one conditional journal write. Only a confirmed CAS may precede dispatch."
  [observation identity event]
  (when-not (identity? identity) (fail "invalid coordination identity"))
  (when-not (event? event) (fail "invalid coordination event"))
  (when-not (observation? observation) (fail "invalid coordination observation"))
  (when (= "error" (:status observation)) (fail "coordination read failed"))
  (let [present (= "present" (:status observation)) document (:document observation)]
    (when (and present (not (document? document))) (fail "invalid coordination document"))
    (when (and present (not= identity (:identity document))) (fail "coordination identity mismatch"))
    (when-not (= (:target_etag event) (when present (:etag observation))) (fail "stale coordination observation"))
    (when (and present (= (:write_id event) (:write_id document))) (fail "coordination write_id reused"))
    (when (and present (== maximum (:revision document))) (fail "coordination revision exhausted"))
    (if present
      {:condition {:if_match (:etag observation)}
       :document (assoc (transition document event) :revision (inc (long (:revision document))) :write_id (:write_id event))}
      (do
        (when-not (= "acquire" (:type event)) (fail "coordination object absent"))
        {:condition {:if_none_match "*"}
         :document {:schema_version 1 :identity identity :revision 1 :write_id (:write_id event)
                    :lock {:state "held" :run_id (:run_id event)} :topology_declared false :nodes {}}}))))

(defn valid-document?
  "Validate an untrusted complete journal without exposing field contents."
  [document]
  (try (boolean (if (= 3 (:schema_version document)) ((requiring-resolve 'io.github.getcolors.compute-managed-journal/managed-document-valid?) document) (document? document))) (catch Exception _ false)))

(defn coordination [observation identity event]
  (cond
    (and (string? (:type event)) (str/starts-with? (:type event) "managed/"))
    ((requiring-resolve 'io.github.getcolors.compute-managed-journal/managed-coordination) observation identity event)
    (and (string? (:type event)) (str/starts-with? (:type event) "lifecycle/"))
    ((requiring-resolve 'io.github.getcolors.compute-lifecycle/lifecycle) observation identity event)
    :else (schema-one-coordination observation identity event)))
