(ns io.github.getcolors.compute-orchestration-test
  (:require [clojure.test :refer [deftest is]]
            [io.github.getcolors.compute-orchestration :as o]
            [io.github.getcolors.compute-coordinator :as c]
            [io.github.getcolors.compute-coordinator-test :as ct]
            [io.github.getcolors.compute-inspection :as inspection]))
(def opts (assoc ct/opts :green/event :create :compute-prevent-destroy false))
(def topology [{:role "broker" :count 2}])
(def requirements {:security {}})
(def fp (str "SHA256:" (apply str (repeat 43 "A"))))
(defn world []
  (let [storage (ct/store) states (atom {}) calls (atom []) failures (atom #{})]
    {:storage storage :states states :calls calls :failures failures
     :deps {:validate-deployment (fn [& _] true)
            :coordinator (fn [opts env] (c/coordinator opts env (:read storage) (:write storage) (ct/ids) {:event-prefix "lifecycle/"}))
            :registration-preflight (fn [& _] {:status "checked"})
            :prepare-keypair (fn [_ ownership _ intent prepared]
                               (when (= "fresh" (:status ownership)) (is (true? (intent))) (is (true? (prepared fp))))
                               {:mode "managed" :public_key "public" :fingerprint fp :private_key_path "/home/test/.ssh/demo"})
            :cleanup-keypair (fn [& _] (swap! calls conj :cleanup) {:cleaned true})
            :state-presence (fn [_ key _] {:status (if (contains? @states key) "present" "absent")})
            :read-state (fn [_ key _] (get @states key {:status "error"}))
            :deployment-requests (fn [_ _ _ key] {:shared {:node_id "shared" :key key}
                                                 :nodes [{:node_id "broker-0" :key key} {:node_id "broker-1" :key key}]})
            :provider-request (fn [& _] {:documents {}})
            :converge-state (fn [_ key _ operation _ _]
                              (swap! calls conj [key operation])
                              (if (= operation "delete")
                                (do (swap! states assoc key {:status "present" :params {} :outputs {} :state_empty true}) {:status "destroyed"})
                                (if (contains? @failures key) {:status "error"}
                                    (let [node-id (second (re-find #"/nodes/(.+)\.tfstate$" key))
                                          params (cond-> {:provider "aws"} node-id (assoc :node_id node-id :name (str "demo-" node-id) :ip (if (= node-id "broker-0") "192.0.2.10" "192.0.2.11") :user "ubuntu" :sudoer "root"))
                                          result {:status "present" :params params :outputs {:params params} :state_empty false}]
                                      (swap! states assoc key result) (assoc result :status "ready")))))}}))
(deftest native-fanout-create-delete-and-recreate-empty-states
  (let [{:keys [deps calls storage]} (world)]
    (is (= "ready" (:status (o/orchestrate opts topology requirements {} deps))))
    (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))
    (is (= "destroyed" (:status (o/orchestrate (assoc opts :green/event :delete) topology requirements {} deps))))
    (is (= :cleanup (last @calls)))
    (is (= "retired" (get-in @(:state storage) [:observation :document :status])))
    (is (= "ready" (:status (o/orchestrate opts topology requirements {} deps))))
    (is (= 2 (get-in @(:state storage) [:observation :document :generation])))))
(deftest failed-siblings-settle-and-orphan-state-refuses-before-key
  (let [{:keys [deps failures calls storage]} (world)]
    (swap! failures conj "demo/compute/nodes/broker-0.tfstate")
    (is (= {:status "error"} (o/orchestrate opts topology requirements {} deps)))
    (is (= 3 (count @calls)))
    (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))
    (is (= "failed" (get-in @(:state storage) [:observation :document :nodes :broker-0 :phase]))))
  (let [{:keys [deps states]} (world)]
    (swap! states assoc "demo/compute/shared.tfstate" {:status "present" :params {:provider "aws"} :outputs {} :state_empty false})
    (is (= {:status "error"} (o/orchestrate opts topology requirements {}
                                                      (assoc deps :prepare-keypair (fn [& _] (throw (AssertionError. "must not touch keys")))))))))

(deftest inspection-reads-idle-inventory-and-refuses-held-journal
  (let [{:keys [deps storage]} (world)]
    (o/orchestrate opts topology requirements {} deps)
    (let [reader {:journal-get (fn [& _] ((:read storage))) :read-state (:read-state deps)}]
      (is (= "present" (:status (inspection/read-deployment opts {"HOME" "/tmp/operator"} reader))))
      (swap! (:state storage) assoc-in [:observation :document :lock] {:state "held" :run_id "foreign"})
      (is (= {:status "error"} (inspection/read-deployment opts {} reader))))))
