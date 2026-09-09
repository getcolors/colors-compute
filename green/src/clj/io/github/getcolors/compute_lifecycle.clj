(ns io.github.getcolors.compute-lifecycle
  "Strict schema-2 lifecycle intentions; successful CAS alone grants dispatch."
  (:require [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-coordination :as coordination]))
(def maximum 9007199254740991)
(defn- fail [message] (throw (ex-info message {})))
(defn- require-transition [value] (when-not value (fail "lifecycle transition refused")))
(defn- exact? [value fields] (and (map? value) (= (set (keys value)) fields)))
(defn- safe? [value] (and (string? value) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value))))
(defn- nonblank? [value] (and (string? value) (not (str/blank? value))))
(defn- integer-in? [value low high] (and (number? value) (<= low value high) (== value (Math/floor (double value)))))
(defn- role? [value] (or (nil? value) (and (string? value) (boolean (re-matches #"[a-z][a-z0-9]*(-[a-z0-9]+)*" value)))))
(defn- fingerprint? [value] (and (string? value) (boolean (re-matches #"SHA256:[A-Za-z0-9+/]{43}" value))))
(defn- running? [record] (contains? #{"running" "destroying"} (:phase record)))
(def idle-shared {:phase "declared" :operation nil :operation_id nil})
(def absent-key {:mode nil :phase "absent" :fingerprint nil})
(defn- record? [{:keys [phase operation operation_id]}]
  (and (contains? #{"declared" "running" "ready" "failed" "destroying" "destroyed"} phase)
       (if (= phase "declared") (and (nil? operation) (nil? operation_id))
           (and (safe? operation_id) (contains? #{"create" "destroy"} operation)
                (case phase ("running" "ready") (= operation "create")
                      ("destroying" "destroyed") (= operation "destroy") true)))))
(defn- records [doc] (cons (:shared doc) (vals (:nodes doc))))
(defn- all-destroyed? [doc] (every? #(= "destroyed" (:phase %)) (records doc)))
(defn- active-work? [doc] (some running? (records doc)))
(defn- document? [doc]
  (and (exact? doc #{:schema_version :identity :revision :write_id :lock :generation :status :topology_declared :key :shared :nodes})
       (number? (:schema_version doc)) (== 2 (:schema_version doc)) (coordination/identity? (:identity doc))
       (integer-in? (:revision doc) 1 maximum) (integer-in? (:generation doc) 1 maximum) (safe? (:write_id doc))
       (contains? #{"active" "deleting" "retired"} (:status doc)) (boolean? (:topology_declared doc))
       (exact? (:lock doc) #{:state :run_id})
       (case (get-in doc [:lock :state]) "held" (safe? (get-in doc [:lock :run_id])) "idle" (nil? (get-in doc [:lock :run_id])) false)
       (let [{:keys [mode phase fingerprint] :as key} (:key doc)]
         (and (exact? key #{:mode :phase :fingerprint}) (contains? #{"absent" "intent" "prepared" "cleanup" "removed"} phase)
              (if (= phase "absent") (and (nil? mode) (nil? fingerprint))
                  (and (contains? #{"managed" "external"} mode)
                       (if (or (= mode "external") (= phase "intent")) (nil? fingerprint) (fingerprint? fingerprint))))))
       (exact? (:shared doc) #{:phase :operation :operation_id}) (record? (:shared doc))
       (map? (:nodes doc)) (<= (count (:nodes doc)) 1000)
       (every? (fn [[id node]]
                 (let [id (if (keyword? id) (subs (str id) 1) id)]
                   (and (safe? id) (exact? node #{:state_key :role :index :desired :phase :operation :operation_id})
                        (role? (:role node)) (integer-in? (:index node) 0 999) (boolean? (:desired node)) (record? node)
                        (= id (str (when (:role node) (str (:role node) "-")) (long (:index node))))
                        (= (:state_key node) (str (get-in doc [:identity :profile]) "/compute/nodes/" id ".tfstate"))))) (:nodes doc))
       (let [ids (keep :operation_id (records doc))] (= (count ids) (count (set ids))))
       (or (not (active-work? doc)) (= "held" (get-in doc [:lock :state])))
       (let [desired (filter :desired (vals (:nodes doc))) groups (group-by :role desired)]
         (and (or (not (contains? groups nil)) (= 1 (count groups)))
              (every? (fn [nodes] (= (mapv long (sort (map :index nodes))) (vec (range (count nodes))))) (vals groups))))
       (or (not= "deleting" (:status doc)) (not-any? :desired (vals (:nodes doc))))
       (or (not= "retired" (:status doc)) (and (= "removed" (get-in doc [:key :phase])) (all-destroyed? doc) (not-any? :desired (vals (:nodes doc)))))
       (or (not (contains? #{"cleanup" "removed"} (get-in doc [:key :phase])))
           (and (contains? #{"deleting" "retired"} (:status doc)) (all-destroyed? doc)))))
(defn valid-document? [doc] (try (boolean (document? doc)) (catch Exception _ false)))
(def extras {"acquire" #{} "release" #{} "begin-delete" #{} "retire" #{} "recreate" #{}
             "declare" #{:topology} "key-intent" #{:mode} "key-prepared" #{:fingerprint} "key-cleanup" #{} "key-removed" #{}
             "shared-start" #{:operation_id} "shared-destroy" #{:operation_id} "shared-complete" #{:operation_id} "shared-fail" #{:operation_id}
             "shared-retry" #{:evidence} "start" #{:node_id :operation_id} "destroy" #{:node_id :operation_id}
             "complete" #{:node_id :operation_id} "fail" #{:node_id :operation_id} "retry" #{:node_id :evidence}})
(defn- event? [event]
  (and (map? event) (string? (:type event)) (str/starts-with? (:type event) "lifecycle/")
       (let [suffix (subs (:type event) 10) additional (get extras suffix)]
         (and additional (exact? event (into #{:type :run_id :write_id :target_etag} additional))
              (safe? (:run_id event)) (safe? (:write_id event)) (or (nil? (:target_etag event)) (nonblank? (:target_etag event)))
              (or (not (contains? event :node_id)) (safe? (:node_id event)))
              (or (not (contains? event :operation_id)) (safe? (:operation_id event)))
              (or (not= suffix "declare") (coordination/topology (:topology event)))
              (or (not= suffix "key-intent") (contains? #{"managed" "external"} (:mode event)))
              (or (not= suffix "key-prepared") (nil? (:fingerprint event)) (fingerprint? (:fingerprint event)))
              (or (not (contains? event :evidence)) (= "readable-state" (:evidence event)))))))
(defn- transition [doc event suffix]
  (let [node-key (when (:node_id event) (keyword (:node_id event))) node (get-in doc [:nodes node-key])
        unique? (not-any? #(= (:operation_id event) (:operation_id %)) (records doc))
        active? (= "active" (:status doc)) prepared? (= "prepared" (get-in doc [:key :phase]))]
    (case suffix
      "declare" (do (require-transition (and active? (not (active-work? doc))))
                    (let [requested (coordination/topology (:topology event)) ids (map #(keyword (:node_id %)) requested)]
                      (require-transition (<= (count (into (set (keys (:nodes doc))) ids)) 1000))
                      (assoc doc :topology_declared true :nodes
                             (reduce (fn [nodes {:keys [node_id role index]}]
                                       (let [id (keyword node_id) old (get nodes id)]
                                         (assoc nodes id (if (and old (not= "destroyed" (:phase old))) (assoc old :desired true)
                                                            (merge idle-shared {:state_key (str (get-in doc [:identity :profile]) "/compute/nodes/" node_id ".tfstate")
                                                                                :role role :index index :desired true})))))
                                     (into {} (map (fn [[id node]] [id (assoc node :desired false)]) (:nodes doc))) requested))))
      "key-intent" (do (require-transition (and active? (= "absent" (get-in doc [:key :phase])))) (assoc doc :key {:mode (:mode event) :phase "intent" :fingerprint nil}))
      "key-prepared" (do (require-transition (and (= "intent" (get-in doc [:key :phase])) (if (= "managed" (get-in doc [:key :mode])) (fingerprint? (:fingerprint event)) (nil? (:fingerprint event)))))
                         (update doc :key assoc :phase "prepared" :fingerprint (:fingerprint event)))
      "shared-start" (do (require-transition (and active? (:topology_declared doc) prepared? (not (active-work? doc)) (contains? #{"declared" "ready"} (get-in doc [:shared :phase])) unique?))
                         (assoc doc :shared {:phase "running" :operation "create" :operation_id (:operation_id event)}))
      "shared-destroy" (do (require-transition (and (every? #(= "destroyed" (:phase %)) (vals (:nodes doc))) (not (contains? #{"running" "destroying" "destroyed"} (get-in doc [:shared :phase]))) unique?))
                           (assoc doc :shared {:phase "destroying" :operation "destroy" :operation_id (:operation_id event)}))
      ("shared-complete" "shared-fail") (do (require-transition (and (running? (:shared doc)) (= (:operation_id event) (get-in doc [:shared :operation_id]))))
                                             (assoc-in doc [:shared :phase] (if (= suffix "shared-fail") "failed" (if (= "create" (get-in doc [:shared :operation])) "ready" "destroyed"))))
      "shared-retry" (do (require-transition (and (= "failed" (get-in doc [:shared :phase])) (= "create" (get-in doc [:shared :operation])))) (assoc doc :shared idle-shared))
      ("start" "destroy") (do (require-transition (and node unique?))
                                (require-transition (if (= suffix "start") (and active? (:desired node) prepared? (= "ready" (get-in doc [:shared :phase])) (contains? #{"declared" "ready"} (:phase node)))
                                                        (and (not (:desired node)) (not (contains? #{"running" "destroying" "destroyed"} (:phase node))))))
                                (update-in doc [:nodes node-key] assoc :phase (if (= suffix "start") "running" "destroying") :operation (if (= suffix "start") "create" "destroy") :operation_id (:operation_id event)))
      ("complete" "fail") (do (require-transition (and node (running? node) (= (:operation_id node) (:operation_id event))))
                                (assoc-in doc [:nodes node-key :phase] (if (= suffix "fail") "failed" (if (= "create" (:operation node)) "ready" "destroyed"))))
      "retry" (do (require-transition (and node (= "failed" (:phase node)) (= "create" (:operation node)))) (update-in doc [:nodes node-key] merge idle-shared))
      "begin-delete" (do (require-transition (and (not= "retired" (:status doc)) (not (active-work? doc))))
                         (assoc doc :status "deleting" :nodes (into {} (map (fn [[id node]] [id (assoc node :desired false)]) (:nodes doc)))))
      "key-cleanup" (do (require-transition (and (= "deleting" (:status doc)) (all-destroyed? doc) prepared?)) (assoc-in doc [:key :phase] "cleanup"))
      "key-removed" (do (require-transition (= "cleanup" (get-in doc [:key :phase]))) (assoc-in doc [:key :phase] "removed"))
      "retire" (do (require-transition (and (= "deleting" (:status doc)) (= "removed" (get-in doc [:key :phase])) (all-destroyed? doc))) (assoc doc :status "retired"))
      "recreate" (do (require-transition (and (= "retired" (:status doc)) (not (active-work? doc)) (< (:generation doc) maximum)))
                     (assoc doc :generation (inc (long (:generation doc))) :status "active" :key absent-key :shared idle-shared :topology_declared false))
      "release" (do (require-transition (and (not (active-work? doc)) (not (contains? #{"intent" "cleanup"} (get-in doc [:key :phase]))))) (assoc doc :lock {:state "idle" :run_id nil}))
      (require-transition false))))
(defn lifecycle [observation identity event]
  (when-not (coordination/identity? identity) (fail "invalid lifecycle identity"))
  (when-not (event? event) (fail "invalid lifecycle event"))
  (when-not (case (:status observation) ("absent" "error") (exact? observation #{:status})
                 "present" (and (exact? observation #{:status :etag :document}) (nonblank? (:etag observation))) false)
    (fail "invalid lifecycle observation"))
  (when (= "error" (:status observation)) (fail "lifecycle read failed"))
  (let [present? (= "present" (:status observation)) doc (:document observation) suffix (subs (:type event) 10)]
    (when (and present? (not (valid-document? doc))) (fail "invalid lifecycle document"))
    (when (and present? (not= identity (:identity doc))) (fail "lifecycle identity mismatch"))
    (when-not (= (:target_etag event) (when present? (:etag observation))) (fail "lifecycle stale observation"))
    (when (and present? (= (:write_id event) (:write_id doc))) (fail "lifecycle write_id reused"))
    (when (and present? (== maximum (:revision doc))) (fail "lifecycle revision exhausted"))
    (if-not present?
      (do (require-transition (= suffix "acquire"))
          {:condition {:if_none_match "*"} :document {:schema_version 2 :identity identity :revision 1 :write_id (:write_id event)
                                                   :lock {:state "held" :run_id (:run_id event)} :generation 1 :status "active" :topology_declared false
                                                   :key absent-key :shared idle-shared :nodes {}}})
      (let [next (if (= suffix "acquire")
                   (do (when (= "held" (get-in doc [:lock :state])) (fail "lifecycle lock held")) (assoc doc :lock {:state "held" :run_id (:run_id event)}))
                   (do (when-not (= "held" (get-in doc [:lock :state])) (fail "lifecycle lock not held"))
                       (when-not (= (:run_id event) (get-in doc [:lock :run_id])) (fail "lifecycle owner mismatch"))
                       (transition doc event suffix)))]
        {:condition {:if_match (:etag observation)} :document (assoc next :revision (inc (long (:revision doc))) :write_id (:write_id event))}))))
