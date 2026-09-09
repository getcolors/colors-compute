(ns io.github.getcolors.compute-managed-journal
 (:require [clojure.string :as str] [io.github.getcolors.compute-coordination :as coordination]))
(def ^:private maximum 9007199254740991)
(defn- exact? [v fields] (and (map? v) (= fields (set (keys v)))))
(defn- safe? [v] (and (string? v) (boolean (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" v))))
(defn- nonblank? [v] (and (string? v) (not (str/blank? v))))
(defn- integer? [v] (and (number? v) (<= 1 v maximum) (== v (Math/floor (double v)))))
(defn- require-valid [v message] (when-not v (throw (ex-info message {}))))
(defn managed-document-valid? [doc]
 (and (exact? doc #{:schema_version :kind :identity :revision :write_id :lock :generation :status :shared}) (= 3 (:schema_version doc)) (= "managed-kubernetes" (:kind doc)) (coordination/identity? (:identity doc)) (integer? (:revision doc)) (integer? (:generation doc)) (safe? (:write_id doc))
      (let [{:keys [lock status shared]} doc {:keys [phase operation operation_id]} shared]
       (and (exact? lock #{:state :run_id}) (or (and (= "held" (:state lock)) (safe? (:run_id lock))) (= lock {:state "idle" :run_id nil}))
            (contains? #{"active" "retired"} status) (exact? shared #{:phase :operation :operation_id}) (contains? #{"declared" "running" "ready" "failed" "destroyed"} phase)
            (if (= phase "declared") (and (nil? operation) (nil? operation_id)) (and (contains? #{"create" "destroy"} operation) (safe? operation_id)))
            (or (not= phase "ready") (= operation "create")) (or (not= phase "destroyed") (= operation "destroy"))
            (or (not= phase "running") (= "held" (:state lock))) (or (not= status "retired") (= phase "destroyed"))))))
(defn managed-coordination [observed identity event]
 (require-valid (coordination/identity? identity) "invalid coordination identity")
 (require-valid (and (map? event) (string? (:type event)) (str/starts-with? (:type event) "managed/")) "invalid managed event")
 (let [kind (subs (:type event) 8) extras {"acquire" #{} "release" #{} "recreate" #{} "shared-start" #{:operation_id} "shared-destroy" #{:operation_id} "shared-complete" #{:operation_id} "shared-fail" #{:operation_id} "shared-retry" #{:evidence}}
       present (= "present" (:status observed)) doc (:document observed)]
  (require-valid (and (contains? extras kind) (exact? event (into #{:type :run_id :write_id :target_etag} (get extras kind))) (safe? (:run_id event)) (safe? (:write_id event)) (or (nil? (:target_etag event)) (nonblank? (:target_etag event))) (or (not (contains? event :operation_id)) (safe? (:operation_id event))) (or (not= kind "shared-retry") (= "readable-state" (:evidence event)))) "invalid managed event")
  (require-valid (and (map? observed) (if present (and (exact? observed #{:status :etag :document}) (nonblank? (:etag observed))) (and (contains? #{"absent" "error"} (:status observed)) (exact? observed #{:status})))) "invalid coordination observation")
  (require-valid (not= "error" (:status observed)) "coordination read failed")
  (when present (require-valid (managed-document-valid? doc) "invalid managed document") (require-valid (= identity (:identity doc)) "coordination identity mismatch") (require-valid (not= (:write_id doc) (:write_id event)) "coordination write_id reused") (require-valid (< (:revision doc) maximum) "coordination revision exhausted"))
  (require-valid (= (:target_etag event) (when present (:etag observed))) "stale coordination observation")
  (if-not present
    (do (require-valid (= kind "acquire") "coordination object absent")
        {:condition {:if_none_match "*"} :document {:schema_version 3 :kind "managed-kubernetes" :identity identity :revision 1 :write_id (:write_id event) :lock {:state "held" :run_id (:run_id event)} :generation 1 :status "active" :shared {:phase "declared" :operation nil :operation_id nil}}})
    (let [r (:shared doc)
          result (if (= kind "acquire")
                   (do (require-valid (= "idle" (get-in doc [:lock :state])) "coordination lock held") (assoc doc :lock {:state "held" :run_id (:run_id event)}))
                   (do (require-valid (= (:lock doc) {:state "held" :run_id (:run_id event)}) "coordination owner mismatch")
                     (case kind
                      "release" (do (require-valid (not= "running" (:phase r)) "coordination operations outstanding") (assoc doc :lock {:state "idle" :run_id nil}))
                      "recreate" (do (require-valid (and (= "retired" (:status doc)) (< (:generation doc) maximum)) "managed transition refused") (assoc doc :status "active" :generation (inc (long (:generation doc))) :shared {:phase "declared" :operation nil :operation_id nil}))
                      ("shared-start" "shared-destroy") (let [operation (if (= kind "shared-start") "create" "destroy") allowed (if (= operation "create") #{"declared" "ready"} #{"declared" "ready" "failed"})]
                        (require-valid (and (= "active" (:status doc)) (contains? allowed (:phase r)) (not= (:operation_id event) (:operation_id r))) "managed transition refused")
                        (assoc doc :shared {:phase "running" :operation operation :operation_id (:operation_id event)}))
                      ("shared-complete" "shared-fail") (let [phase (if (= kind "shared-fail") "failed" (if (= "create" (:operation r)) "ready" "destroyed"))]
                        (require-valid (and (= "running" (:phase r)) (= (:operation_id r) (:operation_id event))) "managed transition refused")
                        (cond-> (assoc-in doc [:shared :phase] phase) (= phase "destroyed") (assoc :status "retired")))
                      "shared-retry" (do (require-valid (and (= "active" (:status doc)) (= "failed" (:phase r)) (= "create" (:operation r))) "managed transition refused") (assoc doc :shared {:phase "declared" :operation nil :operation_id nil})))))
          result (assoc result :revision (inc (long (:revision doc))) :write_id (:write_id event))]
      (require-valid (managed-document-valid? result) "invalid managed transition result")
      {:condition {:if_match (:etag observed)} :document result}))))
