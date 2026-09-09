(ns io.github.getcolors.compute-orchestration
  "Deployment journal ownership around the real Colors fan-out and join."
  (:require [green.workflow :as engine]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-coordination :as coordination]
            [io.github.getcolors.compute-coordinator :as coordinator]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-request :as request]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-key-request :as key-request]
            [io.github.getcolors.compute-planning :as planning]
            [io.github.getcolors.compute-registration :as registration]
            [io.github.getcolors.compute-ssh :as ssh]
            [io.github.getcolors.compute-workflow :as workflow]))
(defn- require-valid [condition] (when-not condition (throw (ex-info "compute lifecycle refused" {}))))
(defn orchestrate
  ([opts topology requirements] (orchestrate opts topology requirements (into {} (System/getenv)) {}))
  ([opts topology requirements environment] (orchestrate opts topology requirements environment {}))
  ([opts topology requirements environment dependencies]
   (let [owner (atom nil) acquired (atom false) keys (atom nil) cancelled (atom nil)]
     (letfn [(call [name default & args] (apply (get dependencies name default) args))
             (snapshot [] (:document (coordinator/snapshot @owner)))
             (transition [type fields] (coordinator/transition! @owner type fields))
             (readable [key]
               (let [result (call :read-state (fn [opts key env] (runtime/read-state opts key env runtime/run-command true)) opts key environment)]
                 (require-valid (and (= "present" (:status result)) (or (= (:provider-compute opts) (get-in result [:params :provider])) (true? (:state_empty result))))) result))
             (node-key [id] (if (nil? id) (:shared @keys) (get-in (compute/state-keys (:profile opts) [id]) [:nodes id])))
             (presence-for [key record]
               (let [presence (call :state-presence execution/state-presence opts key environment)]
                 (require-valid (contains? #{{:status "present"} {:status "absent"}} presence))
                 (if (contains? #{"declared" "destroyed"} (:phase record))
                   (when (= presence {:status "present"}) (require-valid (true? (:state_empty (readable key)))))
                   (require-valid (= presence {:status "present"})))
                 (when (= "failed" (:phase record)) (require-valid (= (:provider-compute opts) (get-in (readable key) [:params :provider])))) presence))
             (outcome [id operation-id success?]
               (transition (if (nil? id) (if success? "shared-complete" "shared-fail") (if success? "complete" "fail"))
                           (cond-> {:operation_id operation-id} (some? id) (assoc :node_id id))))
             (attempt [id documents operation]
               (let [doc (snapshot) record (if (nil? id) (:shared doc) (get-in doc [:nodes (keyword id)])) key (node-key id)]
                 (if (and (= operation "delete") (= "destroyed" (:phase record))) {:status "destroyed"}
                     (let [presence (presence-for key record)]
                       (when (and (= operation "create") (= "failed" (:phase record)))
                         (require-valid (= "create" (:operation record)))
                         (transition (if (nil? id) "shared-retry" "retry") (cond-> {:evidence "readable-state"} (some? id) (assoc :node_id id))))
                       (let [operation-id (transition (if (nil? id) (if (= operation "create") "shared-start" "shared-destroy") (if (= operation "create") "start" "destroy"))
                                                      (if (nil? id) {} {:node_id id}))
                             result (try
                                      (if (and (= operation "delete") (= "declared" (:phase record)) (= presence {:status "absent"})) {:status "destroyed"}
                                          (call :converge-state execution/converge-state opts key documents operation presence environment))
                                      (catch InterruptedException error (coordinator/poison! @owner) (throw error))
                                      (catch Exception error (outcome id operation-id false) (throw error)))
                             success? (= (:status result) (if (= operation "create") "ready" "destroyed"))]
                         (outcome id operation-id success?) (require-valid success?) result)))))
             (execute []
               (let [declarations (coordination/topology topology)
                     operation (get opts :green/event :create) operation (if (keyword? operation) (name operation) operation)
                     declarations (if (true? (:private requirements)) (mapv #(assoc % :private true) declarations) declarations)]
                 (require-valid (and (seq declarations) (contains? #{"create" "delete"} operation) (not (true? (:green/dry-run opts)))))
                 (let [legacy-keys (get requirements :legacy_state_keys [])]
                   (require-valid (and (vector? legacy-keys) (= (count legacy-keys) (count (set legacy-keys)))))
                   (doseq [legacy legacy-keys]
                     (require-valid (= {:status "absent"} (call :legacy-state-presence
                                                               (fn [opts key env] (execution/state-presence opts key env runtime/run-command true))
                                                               opts legacy environment)))))
                 (require-valid (or (= operation "create") (false? (:compute-prevent-destroy opts))))
                 (reset! keys (compute/state-keys (:profile opts) (mapv :node_id declarations)))
                 (reset! owner (call :coordinator (fn [opts env] (coordinator/coordinator opts env nil nil nil {:event-prefix "lifecycle/"})) opts environment))
                 (coordinator/acquire! @owner) (reset! acquired true)
                 (let [initial (snapshot)]
                   (if (and (= operation "delete") (= "retired" (:status initial))) {:status "destroyed"}
                       (do
                         (when (and (= operation "create") (= "retired" (:status initial))) (transition "recreate" {}))
                         (let [doc (snapshot) selected (ssh/mode opts)]
                           (require-valid (if (= operation "create") (= "active" (:status doc)) (contains? #{"active" "deleting"} (:status doc))))
                           (require-valid (contains? #{nil (:mode selected)} (get-in doc [:key :mode])))
                           (let [shared-read (atom nil)]
                             (doseq [[id record] (cons [nil (:shared doc)] (map (fn [[id record]] [(name id) record]) (:nodes doc)))]
                               (let [key (node-key id) presence (presence-for key record)]
                                 (when (= presence {:status "present"})
                                   (let [result (readable key)] (when (nil? id) (reset! shared-read result))))))
                             (if (= operation "create")
                               (do (doseq [node declarations :when (not (contains? (:nodes doc) (keyword (:node_id node))))]
                                     (presence-for (node-key (:node_id node)) {:phase "declared"}))
                                   (coordinator/declare! @owner topology))
                               (transition "begin-delete" {}))
                             (when (= operation "create")
                               (require-valid (true? (call :validate-deployment planning/validate-deployment opts topology requirements))))
                             (let [doc (snapshot) key-record (:key doc)
                                   _ (when (= operation "create")
                                       (call :registration-preflight registration/registration-preflight opts (:mode selected) (get-in @shared-read [:outputs :registration]) nil environment))
                                   _ (require-valid (contains? #{"absent" "prepared"} (:phase key-record)))
                                   ownership (if (= "managed" (:mode key-record)) {:status "prepared" :fingerprint (:fingerprint key-record)} {:status "fresh"})
                                   intent (fn [] (transition "key-intent" {:mode "managed"}) true)
                                   prepared (fn [fingerprint] (transition "key-prepared" {:fingerprint fingerprint}) true)
                                   key (if (= operation "create")
                                         (call :prepare-keypair ssh/prepare-keypair! opts ownership environment intent prepared)
                                         (do (require-valid (= "prepared" (:phase key-record)))
                                             (call :prepare-keypair ssh/prepare-keypair! (assoc opts :green/event :create) ownership environment intent prepared)))]
                               (when (and (= operation "create") (= "external" (:mode key)) (= "absent" (:phase key-record)))
                                 (transition "key-intent" {:mode "external"}) (transition "key-prepared" {:fingerprint nil}))
                               (let [normalized (call :key-request key-request/key-request opts key environment)
                                     assembly (call :deployment-requests deployment/deployment-requests opts topology requirements normalized)
                                     shared-request (:shared assembly) shared-plan (call :provider-request request/provider-request opts "shared" shared-request)
                                     doc (snapshot)
                                     existing (if (and (not= "declared" (get-in doc [:shared :phase]))
                                                       (some #(and (not= "destroyed" (:phase %)) (or (= operation "delete") (not (:desired %)))) (vals (:nodes doc))))
                                                (:outputs (readable (:shared @keys))) {})]
                                 (doseq [[node-id node] (:nodes doc)
                                         :when (and (not= "destroyed" (:phase node)) (or (= operation "delete") (not (:desired node))))]
                                   (let [id (name node-id)]
                                     (if (= "declared" (:phase node)) (attempt id {} "delete")
                                         (let [plan (call :provider-request request/provider-request opts "node" (assoc (dissoc shared-request :name) :node_id id) existing)]
                                           (attempt id (:documents plan) "delete")))))
                                 (if (= operation "delete")
                                   (do (attempt nil (:documents shared-plan) "delete")
                                       (transition "key-cleanup" {})
                                       (call :cleanup-keypair ssh/cleanup-keypair! opts ownership {:all_resources_destroyed true} environment)
                                       (transition "key-removed" {}) (transition "retire" {}) {:status "destroyed"})
                                   (let [shared (attempt nil (:documents shared-plan) "create") requests (into {} (map (juxt :node_id identity) (:nodes assembly)))
                                         callback (fn [values]
                                                    (let [id (get-in values [:colors-compute/request :node_id])]
                                                      (try
                                                        (let [plan (call :provider-request request/provider-request opts "node" (get requests id) (:outputs shared))
                                                              result (attempt id (:documents plan) "create")]
                                                          (assoc values :colors-compute/params (cond-> (:params result) (:private_key_path key) (assoc :ssh_identity_file (:private_key_path key)))))
                                                        (catch InterruptedException error (coordinator/poison! @owner) (throw error))
                                                        (catch Exception _ (assoc values :green/exit 1 :green/err "compute node failed")))))
                                         task (future (call :run engine/run (workflow/cluster-workflow declarations (:node_id (first declarations)) callback) opts))
                                         result (try @task
                                                     (catch InterruptedException error
                                                       (coordinator/poison! @owner)
                                                       ;; Wait for every branch to settle before returning cancellation.
                                                       (loop [] (let [finished (try @task true (catch InterruptedException _ false) (catch Exception _ true))]
                                                                  (when-not finished (recur))))
                                                       (throw error)))]
                                     (require-valid (and (= 0 (:green/exit result)) (contains? result :colors-compute/cluster)))
                                     {:status "ready" :cluster (:colors-compute/cluster result) :shared (:outputs shared) :key (select-keys key [:mode :private_key_path :fingerprint])})))))))))))]
       (let [result (try (execute)
                         (catch InterruptedException error (when @owner (coordinator/poison! @owner)) (reset! cancelled error) {:status "error"})
                         (catch Exception _ {:status "error"}))
             result (if @acquired
                      (try (coordinator/release! @owner) result
                           (catch InterruptedException error (reset! cancelled error) {:status "error"})
                           (catch Exception _ {:status "error"})) result)]
         (if @cancelled (throw @cancelled) result))))))
