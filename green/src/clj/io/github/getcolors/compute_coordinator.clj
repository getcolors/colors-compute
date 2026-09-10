(ns io.github.getcolors.compute-coordinator
  "Invocation-owned serialized journal transitions. Never dispatches provider work."
  (:require [cheshire.core :as json]
            [clojure.string :as str]
            [clojure.walk :as walk]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-coordination :as reducer]
            [io.github.getcolors.compute-lifecycle :as lifecycle]
            [io.github.getcolors.compute-journal :as journal])
  (:import [java.util UUID]
           [java.util.concurrent.locks ReentrantLock]))

(defrecord Coordinator [mutex state identity read-object write-object id-factory reduce-fn event-prefix])
(defn- refuse [message] (throw (ex-info message {})))
(defn- detached [value] (when (some? value) (json/parse-string-strict (json/generate-string value) true)))
(defn- nonblank? [value] (and (string? value) (not (str/blank? value))))
(defn- exact? [value keys] (and (map? value) (= (set (clojure.core/keys value)) keys)))
(defn- same-document? [left right]
  (let [normalize #(walk/postwalk
                    (fn [value]
                      (if (and (number? value) (<= -9007199254740991 value 9007199254740991)
                               (== value (Math/floor (double value))))
                        (long value) value)) (detached %))]
    (= (normalize left) (normalize right))))
(defn- present? [observation]
  (and (exact? observation #{:status :etag :document}) (= "present" (:status observation))
       (nonblank? (:etag observation)) (or (reducer/valid-document? (:document observation)) (lifecycle/valid-document? (:document observation)))))
(defn- observation? [observation]
  (or (and (exact? observation #{:status}) (= "absent" (:status observation)))
      (present? observation)))
(defn ^:no-doc poison! [coordinator]
  (swap! (:state coordinator) assoc :phase :poisoned))
(defmacro ^:private serialized [coordinator & body]
  (let [owner (gensym "coordinator") mutex (gensym "mutex") error (gensym "cancel")]
    `(let [~owner ~coordinator ~mutex (:mutex ~owner)]
       (try
         (.lockInterruptibly ~mutex)
         (try ~@body (finally (.unlock ~mutex)))
         (catch InterruptedException ~error
           (poison! ~owner)
           (throw ~error))))))
(defn- uncertain! [coordinator]
  (poison! coordinator)
  (refuse "coordination ownership uncertain"))
(defn- owned! [coordinator]
  (case (:phase @(:state coordinator))
    :held true
    :poisoned (refuse "coordination ownership uncertain")
    :released (refuse "coordination released")
    (refuse "coordination not acquired")))
(defn- fresh-id! [coordinator]
  (let [value (try ((:id-factory coordinator))
                   (catch InterruptedException error (poison! coordinator) (throw error))
                   (catch Exception _ (refuse "invalid coordination id")))]
    (when-not (and (string? value) (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value)
                   (not (contains? (:used-ids @(:state coordinator)) value)))
      (refuse "invalid coordination id"))
    (swap! (:state coordinator) update :used-ids conj value)
    value))

(defn coordinator
  "Construct without IO. Read takes no args; write receives one detached intent."
  ([opts] (coordinator opts (into {} (System/getenv))))
  ([opts environment] (coordinator opts environment nil nil nil))
  ([opts environment read-object write-object id-factory]
   (coordinator opts environment read-object write-object id-factory {}))
  ([opts environment read-object write-object id-factory options]
   (let [reduce-fn (get options :reducer reducer/coordination)
         event-prefix (get options :event-prefix "")
         ;; Workflow options contain runtime callbacks; only backend identity
         ;; belongs in this private transport snapshot.
         opts (detached (select-keys opts [:profile :provider-compute :provider-backend
                                            :s3-bucket :s3-region :r2-bucket :r2-endpoint]))
         environment (json/parse-string (json/generate-string environment))
         backend (get-in (compute/backend-plan opts (str (:profile opts) "/compute/coordination.json"))
                         [:config :terraform :backend :s3])
         identity {:profile (:profile opts) :provider (:provider-compute opts)
                   :backend (cond-> {:kind (:provider-backend opts) :bucket (:bucket backend) :region (:region backend)}
                              (= "r2" (:provider-backend opts)) (assoc :endpoint (get-in backend [:endpoints :s3])))}]
     (reduce-fn {:status "absent"} identity
                          {:type (str event-prefix "acquire") :run_id "validation" :write_id "validation-write" :target_etag nil})
     (->Coordinator (ReentrantLock.) (atom {:phase :new :attempted? false :observation nil :run-id nil :used-ids #{} :active {}})
                    identity (or read-object #(journal/journal-get opts environment))
                    (or write-object #(journal/journal-put opts % environment))
                    (or id-factory #(str (UUID/randomUUID))) reduce-fn event-prefix))))

(defn snapshot [coordinator]
  (serialized coordinator (detached (:observation @(:state coordinator)))))

(defn- commit! [coordinator event]
  ;; Reducer validation occurs outside the transport catch and preserves ownership.
  (let [intention ((:reduce-fn coordinator) (:observation @(:state coordinator)) (:identity coordinator) event)
        response (try ((:write-object coordinator) (detached intention))
                      (catch InterruptedException error (poison! coordinator) (throw error))
                      (catch Exception _ {:status "error"}))
        observation
        (cond
          (and (exact? response #{:status :etag}) (= "written" (:status response)) (nonblank? (:etag response)))
          {:status "present" :etag (:etag response) :document (:document intention)}

          (and (exact? response #{:status}) (= "error" (:status response)))
          (let [readback (try (detached ((:read-object coordinator)))
                             (catch InterruptedException error (poison! coordinator) (throw error))
                             (catch Exception _ nil))]
            (if (and (present? readback) (same-document? (:document intention) (:document readback)))
              readback (uncertain! coordinator)))

          :else (uncertain! coordinator))]
    (swap! (:state coordinator) assoc :observation (detached observation))
    (when (= :poisoned (:phase @(:state coordinator))) (refuse "coordination ownership uncertain"))
    (detached observation)))

(defn- event! [coordinator type extra]
  (let [state @(:state coordinator)]
    (merge {:type (str (:event-prefix coordinator) type) :run_id (:run-id state) :write_id (fresh-id! coordinator)
            :target_etag (get-in state [:observation :etag])} extra)))

(defn acquire!
  ([coordinator] (acquire! coordinator false))
  ([coordinator require-existing?]
  (serialized coordinator
    (when (:attempted? @(:state coordinator)) (refuse "coordination already acquired"))
    (swap! (:state coordinator) assoc :attempted? true)
    (let [observation (try (detached ((:read-object coordinator)))
                           (catch InterruptedException error (poison! coordinator) (throw error))
                           (catch Exception _ (uncertain! coordinator)))]
      (when-not (observation? observation) (uncertain! coordinator))
      (when-not (boolean? require-existing?) (refuse "existing compute ownership required"))
      (when require-existing?
        (let [doc (:document observation)]
          (when-not (and (= "present" (:status observation)) (= "active" (:status doc))
                         (= "prepared" (get-in doc [:key :phase]))
                         (contains? #{"ready" "failed"} (get-in doc [:shared :phase]))
                         (some #(contains? #{"ready" "failed"} (:phase %)) (vals (:nodes doc))))
            (refuse "existing compute ownership required"))))
      (swap! (:state coordinator) assoc :observation observation :run-id (fresh-id! coordinator))
      (let [result (commit! coordinator (event! coordinator "acquire" {}))]
        (swap! (:state coordinator) assoc :phase :held)
        result)))))

(defn declare! [coordinator topology]
  (serialized coordinator
    (owned! coordinator)
    (commit! coordinator (event! coordinator "declare" {:topology (detached topology)}))))

(defn start! [coordinator node-id]
  (serialized coordinator
    (owned! coordinator)
    (let [operation-id (fresh-id! coordinator)]
      (commit! coordinator (event! coordinator "start" {:node_id node-id :operation_id operation-id}))
      (swap! (:state coordinator) assoc-in [:active node-id] operation-id)
      operation-id)))

(defn- outcome! [coordinator type node-id operation-id]
  (serialized coordinator
    (let [matches? (and (contains? (:active @(:state coordinator)) node-id)
                        (= operation-id (get-in @(:state coordinator) [:active node-id])))]
      (try
        (owned! coordinator)
        (when-not matches? (refuse "coordination local attempt mismatch"))
        (commit! coordinator (event! coordinator type {:node_id node-id :operation_id operation-id}))
        (finally
          (when matches? (swap! (:state coordinator) update :active dissoc node-id)))))))
(defn complete! [coordinator node-id operation-id] (outcome! coordinator "complete" node-id operation-id))
(defn fail! [coordinator node-id operation-id] (outcome! coordinator "fail" node-id operation-id))
(defn release! [coordinator]
  (serialized coordinator
    (owned! coordinator)
    (when (seq (:active @(:state coordinator))) (refuse "coordination operations outstanding"))
    (let [result (commit! coordinator (event! coordinator "release" {}))]
      (swap! (:state coordinator) assoc :phase :released)
      result)))

(defn transition!
  "Commit an additional lifecycle event. Starts return a locally tracked attempt ID."
  ([coordinator type] (transition! coordinator type {}))
  ([coordinator type extra]
   (case type
     "acquire" (acquire! coordinator)
     "release" (release! coordinator)
     (serialized coordinator
       (let [start? (contains? #{"start" "destroy" "shared-start" "shared-destroy"} type)
             outcome? (contains? #{"complete" "fail" "shared-complete" "shared-fail"} type)
             shared? (str/starts-with? type "shared-")
             slot (if shared? ::shared (:node_id extra))
             matches? (and outcome? (contains? (:active @(:state coordinator)) slot)
                           (= (:operation_id extra) (get-in @(:state coordinator) [:active slot])))]
         (try
           (owned! coordinator)
           (when (and outcome? (not matches?)) (refuse "coordination local attempt mismatch"))
           (let [operation-id (when start? (fresh-id! coordinator))
                 extra (cond-> (detached extra) start? (assoc :operation_id operation-id))
                 result (commit! coordinator (event! coordinator type extra))]
             (if start?
               (do (swap! (:state coordinator) assoc-in [:active slot] operation-id) operation-id)
               result))
           (finally (when matches? (swap! (:state coordinator) update :active dissoc slot)))))))))
