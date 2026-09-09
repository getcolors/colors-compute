(ns io.github.getcolors.compute-coordinator-test
  (:require [clojure.test :refer [deftest is]]
            [io.github.getcolors.compute-coordinator :as c]
            [io.github.getcolors.compute-lifecycle :as lifecycle]))

(def opts {:profile "demo" :provider-compute "aws" :provider-backend "s3" :s3-bucket "states" :s3-region "eu-west-1"})
(defn ids []
  (let [counter (atom 0)] #(str "id-" (swap! counter inc))))
(defn store []
  (let [state (atom {:observation {:status "absent"} :writes 0 :reads 0 :fault nil})]
    {:state state
     :read (fn [] (swap! state update :reads inc) (:observation @state))
     :write
     (fn [intent]
       (locking state
         (let [{:keys [observation fault]} @state]
           (swap! state update :writes inc)
           (cond
             (= fault :cancel) (throw (InterruptedException. "sensitive cancellation detail"))
             (= fault :conflict) {:status "conflict"}
             (= fault :unmatched) {:status "error"}
             (= fault :unknown) {:status "unexpected" :detail "sensitive detail"}
             (not (or (and (= {:if_none_match "*"} (:condition intent)) (= "absent" (:status observation)))
                      (and (= (:etag observation) (get-in intent [:condition :if_match])) (= "present" (:status observation)))))
             {:status "conflict"}
             :else
             (let [etag (str "etag-" (:writes @state))]
               (swap! state assoc :observation {:status "present" :etag etag :document (:document intent)})
               (case fault
                 :lost {:status "error"}
                 :throw-after (throw (ex-info "sensitive callback detail" {}))
                 {:status "written" :etag etag}))))))}))
(defn coordinator [storage]
  (c/coordinator opts {} (:read storage) (:write storage) (ids)))

(deftest constructor-and-lifecycle-boundaries
  (let [storage (store) instance (coordinator storage)]
    (is (= 0 (:reads @(:state storage))))
    (is (nil? (c/snapshot instance)))
    (is (thrown-with-msg? Exception #"coordination not acquired" (c/declare! instance [{}])))
    (c/acquire! instance)
    (is (thrown-with-msg? Exception #"coordination already acquired" (c/acquire! instance)))
    (let [before (c/snapshot instance)
          changed (assoc-in before [:document :lock :state] "foreign")]
      (is (not= changed (c/snapshot instance)))
      (is (= before (c/snapshot instance))))
    (c/release! instance)
    (is (= "idle" (get-in (c/snapshot instance) [:document :lock :state])))
    (is (thrown-with-msg? Exception #"coordination released" (c/declare! instance [{}])))))

(deftest exactly-one-acquirer-wins-under-contention
  (let [storage (store) barrier (promise) readers (atom 0)
        read (fn []
               (let [observation ((:read storage))]
                 (when (= 2 (swap! readers inc)) (deliver barrier true))
                 (when-not (deref barrier 3000 false) (throw (ex-info "barrier timeout" {})))
                 observation))
        a (c/coordinator opts {} read (:write storage) (ids))
        b (c/coordinator opts {} read (:write storage) (ids))
        results (mapv deref [(future (try (c/acquire! a) :won (catch Exception _ :lost)))
                             (future (try (c/acquire! b) :won (catch Exception _ :lost)))])]
    (is (= {:won 1 :lost 1} (frequencies results)))
    (is (= 2 (:writes @(:state storage))))
    (is (= "held" (get-in @(:state storage) [:observation :document :lock :state])))))

(deftest concurrent-node-transitions-serialize-and-retain-failed-siblings
  (let [storage (store) instance (coordinator storage)]
    (c/acquire! instance)
    (c/declare! instance [{:count 4}])
    (let [operations (->> (range 4) (mapv #(future [(str %) (c/start! instance (str %))])) (mapv deref))]
      (is (= 4 (count (set (map second operations)))))
      (is (thrown-with-msg? Exception #"coordination operations outstanding" (c/release! instance)))
      (is (thrown-with-msg? Exception #"coordination local attempt mismatch" (c/complete! instance "0" "wrong")))
      (doseq [completion (mapv (fn [[node operation]] (future (if (= node "1") (c/fail! instance node operation) (c/complete! instance node operation)))) operations)]
        @completion)
      (c/release! instance)
      (let [nodes (get-in (c/snapshot instance) [:document :nodes])]
        (is (= "failed" (get-in nodes [(keyword "1") :phase])))
        (is (= 3 (count (filter #(= "ready" (:phase %)) (vals nodes)))))))))

(deftest lost-write-response-confirms-only-exact-document
  (doseq [fault [:lost :throw-after]]
    (let [storage (store) instance (coordinator storage)]
      (swap! (:state storage) assoc :fault fault)
      (is (= "present" (:status (c/acquire! instance))))
      (is (= 1 (:writes @(:state storage))))
      (is (= 2 (:reads @(:state storage))))))
  (doseq [fault [:unmatched :conflict :unknown]]
    (let [storage (store) instance (coordinator storage)]
      (swap! (:state storage) assoc :fault fault)
      (is (thrown-with-msg? Exception #"^coordination ownership uncertain$" (c/acquire! instance)))
      (is (thrown-with-msg? Exception #"^coordination ownership uncertain$" (c/release! instance)))
      (is (= 1 (:writes @(:state storage))))
      (is (= (if (= fault :unmatched) 2 1) (:reads @(:state storage)))))))

(deftest reducer-errors-do-not-poison-but-outcome-conflicts-do
  (let [storage (store) instance (coordinator storage)]
    (c/acquire! instance)
    (c/declare! instance [{:count 2}])
    (is (thrown-with-msg? Exception #"coordination node undeclared" (c/start! instance "missing")))
    (let [first-op (c/start! instance "0") second-op (c/start! instance "1")]
      (swap! (:state storage) assoc :fault :conflict)
      (is (thrown-with-msg? Exception #"coordination ownership uncertain" (c/complete! instance "0" first-op)))
      (is (thrown-with-msg? Exception #"coordination ownership uncertain" (c/fail! instance "1" second-op)))
      (is (= {} (:active @(:state instance))))
      (is (= "running" (get-in (c/snapshot instance) [:document :nodes (keyword "0") :phase]))))))

(deftest cancellation-poisons-and-id-factory-fails-closed
  (let [storage (store) instance (coordinator storage)]
    (swap! (:state storage) assoc :fault :cancel)
    (is (thrown? InterruptedException (c/acquire! instance)))
    (is (thrown-with-msg? Exception #"coordination ownership uncertain" (c/release! instance)))
    (is (= 1 (:writes @(:state storage)))))
  (doseq [factory [(constantly "same-id") (constantly "bad/id") #(throw (ex-info "secret factory detail" {}))]]
    (let [storage (store) instance (c/coordinator opts {} (:read storage) (:write storage) factory)]
      (is (thrown-with-msg? Exception #"^invalid coordination id$" (c/acquire! instance)))
      (is (zero? (:writes @(:state storage)))))))

(deftest matching-run-id-cannot-reacquire-held-journal
  (let [storage (store) owner (coordinator storage)
        values (atom ["id-1" "different-write"])
        factory #(let [value (first @values)] (swap! values next) value)]
    (c/acquire! owner)
    (let [contender (c/coordinator opts {} (:read storage) (:write storage) factory)]
      (is (thrown-with-msg? Exception #"coordination lock held" (c/acquire! contender)))
      (is (= 1 (:writes @(:state storage)))))))

(deftest cancellation-while-queued-poisons-invocation
  (let [storage (store) gate (promise) entered (promise)
        write (fn [intent]
                (when (= 2 (get-in intent [:document :revision]))
                  (deliver entered true)
                  (when-not (deref gate 3000 false) (throw (ex-info "test gate" {}))))
                ((:write storage) intent))
        instance (c/coordinator opts {} (:read storage) write (ids))]
    (c/acquire! instance)
    (let [holder (future (try (c/declare! instance [{}]) :unexpected
                             (catch Exception error (.getMessage error))))
          waiting (promise) cancelled (promise)
          thread (Thread. (fn []
                            (deliver waiting true)
                            (try (c/release! instance) (deliver cancelled :unexpected)
                                 (catch InterruptedException _ (deliver cancelled :cancelled))
                                 (catch Exception _ (deliver cancelled :wrong-error)))))]
      (is (deref entered 3000 false))
      (.start thread)
      (is (deref waiting 3000 false))
      (.interrupt thread)
      (is (= :cancelled (deref cancelled 3000 :timeout)))
      (deliver gate true)
      (is (= "coordination ownership uncertain" (deref holder 3000 :timeout)))
      (.join thread 1000)
      (is (= "held" (get-in (c/snapshot instance) [:document :lock :state])))
      (is (thrown-with-msg? Exception #"coordination ownership uncertain" (c/start! instance "0"))))))

(deftest schema-two-transitions-track-shared-and-node-attempts
  (let [storage (store)
        instance (c/coordinator opts {} (:read storage) (:write storage) (ids)
                                {:reducer lifecycle/lifecycle :event-prefix "lifecycle/"})]
    (is (= 2 (get-in (c/acquire! instance) [:document :schema_version])))
    (c/declare! instance [{:count 1}])
    (c/transition! instance "key-intent" {:mode "external"})
    (c/transition! instance "key-prepared" {:fingerprint nil})
    (let [attempt (c/transition! instance "shared-start")]
      (is (string? attempt))
      (is (thrown-with-msg? Exception #"coordination operations outstanding" (c/release! instance)))
      (swap! (:state storage) assoc :fault :lost)
      (is (= "ready" (get-in (c/transition! instance "shared-complete" {:operation_id attempt}) [:document :shared :phase])))
      (swap! (:state storage) assoc :fault nil))
    (let [attempt (c/start! instance "0")]
      (c/complete! instance "0" attempt))
    (c/transition! instance "begin-delete")
    (let [attempt (c/transition! instance "destroy" {:node_id "0"})]
      (c/complete! instance "0" attempt))
    (let [attempt (c/transition! instance "shared-destroy")]
      (c/transition! instance "shared-complete" {:operation_id attempt}))
    (c/transition! instance "key-cleanup")
    (c/transition! instance "key-removed")
    (c/transition! instance "retire")
    (is (= "idle" (get-in (c/release! instance) [:document :lock :state])))
    (is (lifecycle/valid-document? (:document (c/snapshot instance))))))
