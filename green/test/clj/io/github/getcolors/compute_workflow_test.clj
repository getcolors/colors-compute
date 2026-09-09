(ns io.github.getcolors.compute-workflow-test
  (:require [clojure.test :refer [deftest is]]
            [green.workflow :as workflow]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-workflow :as adapter]))

(defn node [request]
  (assoc request :provider "aws" :name (str "demo-" (:node_id request))
         :ip "192.0.2.1" :user "ubuntu" :sudoer "ubuntu"
         :metadata {:zone "example"}))

(deftest sdk-fan-out-joins-complete-results
  (let [requests (compute/expand [{:role "broker" :count 2}])
        released (promise)
        arrived (atom [])
        callback (fn [opts]
                   (let [request (:colors-compute/request opts)]
                   (if (= 0 (:index request))
                     (when (= ::timeout (deref released 3000 ::timeout))
                       (throw (ex-info "SDK failed to run branches concurrently" {})))
                     (do (swap! arrived conj (:node_id request)) (deliver released true)))
                   (when (= 0 (:index request)) (swap! arrived conj (:node_id request)))
                   (assoc opts :colors-compute/params (node request))))
        result (workflow/run (adapter/cluster-workflow requests "broker-0" callback) {})]
    (is (= 0 (:green/exit result)))
    (is (= ["broker-1" "broker-0"] @arrived))
    (is (= ["broker-0" "broker-1"] (mapv :node_id (get-in result [:colors-compute/cluster :nodes]))))
    (is (= [{:zone "example"} {:zone "example"}]
           (mapv :metadata (get-in result [:colors-compute/cluster :nodes]))))))

(deftest same-callback-handles-single-node
  (let [requests (compute/expand [{:count 1}])
        calls (atom [])
        result (workflow/run (adapter/cluster-workflow requests "0"
                              (fn [opts] (let [request (:colors-compute/request opts)]
                                            (swap! calls conj request)
                                            (assoc opts :colors-compute/params (node request)))))
                             {:green/branches [{:unrelated "parent branch"}]})]
    (is (= 0 (:green/exit result)))
    (is (= requests @calls))
    (is (= ["0"] (mapv :node_id (get-in result [:colors-compute/cluster :nodes]))))))

(deftest failed-branch-prevents-downstream
  (doseq [throw? [false true]]
    (let [requests (compute/expand [{:count 2}])
          downstream (atom false)
          cluster (adapter/cluster-workflow requests "0"
                    (fn [opts]
                      (let [request (:colors-compute/request opts)]
                      (if (= 1 (:index request))
                        (if throw? (throw (ex-info "node failed" {}))
                            (assoc opts :green/exit 7 :green/err "node failed"))
                        (assoc opts :colors-compute/params (node request))))))
          parent (workflow/workflow
                   {:start :test/cluster
                    :wire-fn (fn [step _]
                               (case step
                                 :test/cluster [(workflow/step cluster) :test/downstream]
                                 :test/downstream [(fn [opts] (reset! downstream true) opts)]))})
          result (workflow/run parent {:colors-compute/cluster {:stale true}})]
      (is (pos? (:green/exit result)))
      (is (false? @downstream))
      (is (nil? (:colors-compute/cluster result))))))

(deftest incomplete-successful-node-stops-join
  (let [requests (compute/expand [{:count 2}])
        result (workflow/run (adapter/cluster-workflow requests "0"
                               #(assoc % :colors-compute/params (dissoc (node (:colors-compute/request %)) :user))) {})]
    (is (pos? (:green/exit result)))
    (is (nil? (:colors-compute/cluster result)))))

(deftest optional-downstream-receives-only-complete-cluster
  (let [requests (compute/expand [{:count 2}])
        result (workflow/run
                (adapter/cluster-workflow requests "0"
                  #(assoc % :colors-compute/params (node (:colors-compute/request %)))
                  #(assoc % :test/inventory (mapv :node_id (get-in % [:colors-compute/cluster :nodes])))) {})]
    (is (= 0 (:green/exit result)))
    (is (= ["0" "1"] (:test/inventory result)))))

(deftest negative-or-invalid-node-exit-never-runs-downstream
  (doseq [exit [-1 nil "bad" false -2.5]]
    (let [downstream (atom false)
          result (workflow/run
                  (adapter/cluster-workflow [{:node_id "0" :role nil :index 0}] "0"
                    #(assoc % :green/exit exit)
                    #(do (reset! downstream true) %)) {})]
      (is (= 1 (:green/exit result)))
      (is (false? @downstream))
      (is (nil? (:colors-compute/cluster result))))))
