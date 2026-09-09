(ns io.github.getcolors.compute-coordination-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [clojure.walk :as walk]
            [io.github.getcolors.compute-coordination :as c]))

(def identity {:profile "demo" :provider "aws" :backend {:kind "s3" :bucket "states" :region "eu-west-1"}})
(defn event [type etag write-id]
  {:type type :run_id "run-1" :write_id write-id :target_etag etag})
(defn observation [document etag] {:status "present" :etag etag :document document})
(defn normalize [value]
  (walk/postwalk #(if (and (number? %) (<= -9007199254740991 % 9007199254740991)
                           (== % (Math/floor (double %)))) (long %) %)
                 (json/parse-string (json/generate-string value) true)))

(deftest shared-contract-fixtures
  (doseq [{:keys [name args expected]} (json/parse-string (slurp "../test/fixtures/coordination.json") true)]
    (testing name
      (let [before (pr-str args)
            actual (try (apply c/coordination args) (catch Exception e {:error (.getMessage e)}))]
        (is (= expected (normalize actual)))
        (is (= before (pr-str args)))))))

(deftest ordered-node-transitions-retain-siblings-and-history
  (let [acquired (:document (c/coordination {:status "absent"} identity (event "acquire" nil "write-1")))
        declared (:document (c/coordination (observation acquired "etag-1") identity
                              (assoc (event "declare" "etag-1" "write-2") :topology [{:role "broker" :count 2.0}])))
        started (:document (c/coordination (observation declared "etag-2") identity
                             (assoc (event "start" "etag-2" "write-3") :node_id "broker-0" :operation_id "op-1")))
        completed (:document (c/coordination (observation started "etag-3") identity
                               (assoc (event "complete" "etag-3" "write-4") :node_id "broker-0" :operation_id "op-1")))
        released (:document (c/coordination (observation completed "etag-4") identity (event "release" "etag-4" "write-5")))
        reacquired (:document (c/coordination (observation released "etag-5") identity (event "acquire" "etag-5" "write-6")))]
    (is (= {} (:nodes acquired)))
    (is (= "declared" (get-in declared [:nodes "broker-0" :phase])))
    (is (= "running" (get-in started [:nodes "broker-0" :phase])))
    (is (= "ready" (get-in completed [:nodes "broker-0" :phase])))
    (is (= (get-in declared [:nodes "broker-1"]) (get-in reacquired [:nodes "broker-1"])))
    (is (= (:nodes completed) (:nodes reacquired)))
    (is (= 6 (:revision reacquired)))
    (is (= {:state "idle" :run_id nil} (:lock released)))))

(deftest strict-documents-reject-secret-extensions-and-unsafe-keys
  (let [acquired (:document (c/coordination {:status "absent"} identity (event "acquire" nil "write-1")))
        declared (:document (c/coordination (observation acquired "etag-1") identity
                              (assoc (event "declare" "etag-1" "write-2") :topology [{}])))]
    (doseq [document [(assoc acquired :secret "do-not-emit")
                      (assoc-in acquired [:identity :backend :secret] "do-not-emit")
                      (assoc-in declared [:nodes "0" :credentials] "do-not-emit")
                      (assoc declared :nodes {(keyword "unsafe/0") (get-in declared [:nodes "0"])})]]
      (is (thrown-with-msg? Exception #"^invalid coordination document$"
                            (c/coordination (observation document "etag-2") identity (event "release" "etag-2" "write-3")))))))
