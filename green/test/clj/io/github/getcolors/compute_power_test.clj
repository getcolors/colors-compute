(ns io.github.getcolors.compute-power-test
 (:require [clojure.test :refer [deftest is]] [cheshire.core :as json]
           [io.github.getcolors.compute-power :as power]
           [io.github.getcolors.compute-orchestration :as orchestration]
           [io.github.getcolors.compute-orchestration-test :as ot]))
(def instance-id "ocid1.instance.oc1.example")
(deftest singleton-power-holds-lease-and-refreshes-address
 (let [{:keys [deps storage states]} (ot/world) opts (assoc ot/opts :provider-compute "oci" :oci-config-file-profile "OPERATOR")]
  (is (= "ready" (:status (orchestration/orchestrate ot/opts [{:count 1}] ot/requirements {} deps))))
  (swap! (:state storage) assoc-in [:observation :document :identity :provider] "oci")
  (let [state-key "demo/compute/nodes/0.tfstate"
        params (assoc (get-in @states [state-key :params]) :provider "oci" :provider_id instance-id)
        calls (atom 0)
        power-deps {:journal-get (fn [& _] ((:read storage))) :journal-put (fn [_ intent _] ((:write storage) intent))
                    :read-state (fn [& _] {:status "present" :params params})
                    :provider-power (fn [_ action id _] (swap! calls inc)
                                      (is (= "held" (get-in @(:state storage) [:observation :document :lock :state])))
                                      (is (= instance-id id)) (is (= "start" action)) {:status "ready" :ip "203.0.113.20"})}
        result (power/power-deployment opts "start" {} power-deps)]
    (is (= "ready" (:status result))) (is (= "203.0.113.20" (get-in result [:cluster :nodes 0 :ip])))
    (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))
    (is (= {:status "error"} (power/power-deployment opts "stop" {} (assoc power-deps :provider-power (fn [& _] {:status "error"})))))
    (is (= "held" (get-in @(:state storage) [:observation :document :lock :state])))
    (is (= {:status "error"} (power/power-deployment opts "start" {} power-deps))) (is (= 1 @calls)))))
(deftest oci-soft-stop-uses-explicit-profile-and-sanitized-environment
 (let [calls (atom []) env {"HOME" "/temporary" "OCI_CLI_AUTH" "security_token" "OCI_CLI_ENDPOINT" "https://untrusted.invalid" "TF_LOG" "TRACE"}
       result (power/provider-power {:provider-compute "oci" :oci-config-file-profile "OPERATOR"} "stop" instance-id env
                {:runner (fn [args cwd actual timeout]
                           (swap! calls conj args)
                           (is (= ["oci" "--config-file" "/temporary/.oci/config" "--profile" "OPERATOR"] (subvec args 0 5)))
                           (is (nil? (get actual "TF_LOG"))) (is (nil? (get actual "OCI_CLI_ENDPOINT")))
                           (is (= "security_token" (get actual "OCI_CLI_AUTH")))
                           {:exit 0 :out (json/generate-string {:data {:id instance-id :lifecycle-state (if (= 1 (count @calls)) "RUNNING" "STOPPED")}})})})]
   (is (= {:status "ready"} result)) (is (= 3 (count @calls))) (is (some #{"SOFTSTOP"} (second @calls)))))
(deftest vultr-one-mutation-with-bounded-poll
 (let [id "12345678-1234-1234-1234-123456789abc" calls (atom []) states (atom ["stopped" "stopped" "running"])
       result (power/provider-power {:provider-compute "vultr"} "start" id {"COLORS_PAR_VULTR_API_KEY" "synthetic-token"}
                {:sleep (fn [_] nil) :http (fn [method url headers]
                 (swap! calls conj [method url]) (is (= "Bearer synthetic-token" (get headers "Authorization")))
                 (if (= "POST" method) "" (let [state (first @states)] (swap! states subvec 1) (json/generate-string {:instance {:id id :power_status state :main_ip "203.0.113.9"}}))))})]
   (is (= {:status "ready" :ip "203.0.113.9"} result)) (is (= ["GET" "POST" "GET" "GET"] (mapv first @calls)))))
(deftest unsupported-and-build-are-offline
 (is (= {:status "error"} (power/power-deployment {:provider-compute "aws"} "start" {} {})))
 (is (= {:status "planned" :action "start"} (power/power-deployment {:provider-compute "oci" :green/event :build} "start" {} {}))))
