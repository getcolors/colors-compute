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
  (let [storage (ct/store) states (atom {}) calls (atom []) failures (atom #{}) id-factory (ct/ids)]
    {:storage storage :states states :calls calls :failures failures
     :deps {:validate-deployment (fn [& _] true) :compute-credential-errors (fn [& _] [])
            :coordinator (fn [opts env] (c/coordinator opts env (:read storage) (:write storage) id-factory {:event-prefix "lifecycle/"}))
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
      (is (= "broker-1" (get-in (inspection/read-deployment opts {} reader {:entry_node_id "broker-1"}) [:cluster :entry_node_id])))
      (is (= {:status "error"} (inspection/read-deployment opts {} reader {:entry_node_id "missing"})))
      (swap! (:state storage) assoc-in [:observation :document :lock] {:state "held" :run_id "foreign"})
      (is (= {:status "error"} (inspection/read-deployment opts {} reader))))))

(defn role-world []
  (let [{:keys [deps states] :as w} (world) renders (atom [])
        role-policy {:broker {:security {}}}
        converge (:converge-state deps)]
    (assoc w :renders renders
      :deps (assoc deps
        :deployment-requests (fn [_ _ _ key] {:shared {:node_id "shared" :key key :roles role-policy}
                                              :entry_node_id "broker-1"
                                              :nodes [{:node_id "broker-0" :role "broker" :key key} {:node_id "broker-1" :role "broker" :key key}]})
        :provider-request (fn [_ stage request & _] (when (= stage "shared") (swap! renders conj request)) {:documents {}})
        :converge-state (fn [& args]
                          (let [result (apply converge args) key (second args)]
                            (if (and (= "ready" (:status result)) (get-in result [:params :node_id]))
                              (let [result (assoc-in result [:params :vpc_ip] (if (= "broker-0" (get-in result [:params :node_id])) "10.2.0.10" "10.2.0.11"))
                                    result (assoc-in result [:outputs :params] (:params result))]
                                (swap! states assoc key (assoc result :status "present")) result)
                              result)))))))
(deftest peer-rules-converge-after-native-join-and-preserve-observed-peers
  (let [{:keys [deps renders calls]} (role-world)
        first-result (o/orchestrate opts topology requirements {} deps)]
    (is (= "ready" (:status first-result)))
    (is (= "broker-1" (get-in first-result [:cluster :entry_node_id])))
    (is (= {} (:peers (first @renders))))
    (is (= {:broker-0 {:role "broker" :vpc_ip "10.2.0.10"} :broker-1 {:role "broker" :vpc_ip "10.2.0.11"}} (:peers (second @renders))))
    (is (= ["demo/compute/shared.tfstate" "create"] (last @calls)))
    (reset! renders [])
    (is (= "ready" (:status (o/orchestrate opts topology requirements {} deps))))
    (is (= (:peers (first @renders)) (:peers (second @renders))))))
(deftest failed-peer-prevents-final-shared-attempt
  (let [{:keys [deps failures renders calls]} (role-world)]
    (swap! failures conj "demo/compute/nodes/broker-1.tfstate")
    (is (= {:status "error"} (o/orchestrate opts topology requirements {} deps)))
    (is (= 1 (count @renders)))
    (is (= 1 (count (filter #(= ["demo/compute/shared.tfstate" "create"] %) @calls))))))
(deftest public-singleton-runtime-and-inspection-without-private-network
  (let [{:keys [deps storage states]} (world)
        opts (assoc opts :provider-compute "vultr")
        topology [{:role nil :count 1}]
        requirements {:single_host true :private false :network {:mode "none"}
                      :security {:egress "all" :private_filter false
                                 :ingress [{:id "ssh" :protocol "tcp" :from_port 22 :to_port 22 :sources ["192.0.2.1/32"]}]}}
        converge (:converge-state deps)
        deps (assoc deps
               :deployment-requests (fn [_ _ _ key] {:shared {:node_id "shared" :key key} :nodes [{:node_id "0" :key key}]})
               :converge-state (fn [& args]
                                (let [result (apply converge args)]
                                  (if (= "ready" (:status result))
                                    (let [params (cond-> (assoc (:params result) :provider "vultr")
                                                   (:node_id (:params result)) (assoc :vpc_ip nil))
                                          result (assoc result :params params :outputs {:params params})]
                                      (swap! states assoc (second args) (assoc result :status "present")) result)
                                    result))))
        result (o/orchestrate opts topology requirements {} deps)
        reader {:journal-get (fn [& _] ((:read storage))) :read-state (:read-state deps)}]
    (is (= "ready" (:status result)))
    (is (nil? (get-in result [:cluster :nodes 0 :vpc_ip])))
    (is (not (contains? (get-in result [:shared :params]) :network_cidr)))
    (is (= "present" (:status (inspection/read-deployment opts {} reader requirements))))
    (is (= "ready" (:status (o/orchestrate opts topology requirements {} deps))))
    (is (= "destroyed" (:status (o/orchestrate (assoc opts :green/event :delete) topology requirements {} deps))))))

(deftest existing-state-guard-refuses-without-writes-and-allows-owned-convergence
  (let [{:keys [deps calls storage]} (world)
        guarded (assoc opts :compute-require-existing-state true)
        run #(o/orchestrate guarded topology requirements {} deps)]
    (is (= {:status "error"} (run)))
    (is (= 0 (:writes @(:state storage))))
    (is (empty? @calls))
    (let [owner ((:coordinator deps) opts {})]
      (c/acquire! owner) (c/release! owner))
    (let [before (:writes @(:state storage))]
      (is (= {:status "error"} (run)))
      (is (= before (:writes @(:state storage)))))
    (is (= "ready" (:status (o/orchestrate opts topology requirements {} deps))))
    (is (= "ready" (:status (run))))
    (is (= {:status "destroyed"} (o/orchestrate (assoc guarded :green/event :delete) topology requirements {} deps)))
    (reset! calls [])
    (let [before (:writes @(:state storage))]
      (is (= {:status "error"} (run)))
      (is (= before (:writes @(:state storage))))
      (is (empty? @calls)))
    (is (= {:status "destroyed"} (o/orchestrate (assoc guarded :green/event :delete) topology requirements {} deps)))))

(deftest existing-state-guard-requires-boolean
  (doseq [value [nil "true" 1 []]]
    (let [{:keys [deps storage]} (world) input (assoc opts :compute-require-existing-state value)]
      (is (some #{":compute-require-existing-state must be a boolean"} (io.github.getcolors.compute/validate input)))
      (is (= {:status "error"} (o/orchestrate input topology requirements {} deps)))
      (is (= 0 (:writes @(:state storage)))))))

(deftest recovered-declared-empty-nodes-delete-without-convergence
  (doseq [empty? [true false nil]]
    (let [{:keys [deps storage calls]} (world)]
      (is (= "ready" (:status (o/orchestrate opts topology requirements {} deps))))
      (swap! (:state storage) update-in [:observation :document :nodes]
             #(into {} (map (fn [[id node]] [id (assoc node :phase "declared" :operation nil :operation_id nil)]) %)))
      (let [read-state (fn [opts key env] (if (re-find #"/nodes/" key) {:status "present" :state_empty empty?} ((:read-state deps) opts key env)))
            deps (assoc deps :read-state read-state)]
        (is (= {:status (if (true? empty?) "partial" "error")}
               (inspection/read-deployment opts {} {:journal-get (fn [& _] ((:read storage))) :read-state read-state})))
        (reset! calls [])
        (is (= {:status (if (true? empty?) "destroyed" "error")}
               (o/orchestrate (assoc opts :green/event :delete) topology requirements {} deps)))
        (is (not-any? #(and (vector? %) (re-find #"/nodes/" (first %))) @calls))))))
