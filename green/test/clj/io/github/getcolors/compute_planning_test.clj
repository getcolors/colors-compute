(ns io.github.getcolors.compute-planning-test
  (:require [clojure.test :refer [deftest is]]
            [cheshire.core :as json]
            [io.github.getcolors.compute-planning :as planning]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-key-request :as key]))
(def fixtures (json/parse-string-strict (slurp "../test/fixtures/provider-requests.json") true))
(deftest all-eight-provider-planning-is-credential-free
  (doseq [{:keys [args]} (filter #(= "shared" (second (:args %))) fixtures)]
    (let [[opts _ request] args requirements (select-keys request [:network :security])
          before (pr-str [opts requirements]) result (planning/plan-deployment opts [{:count 2}] requirements)]
      (is (= "planned" (:status result)))
      (is (= 2 (count (get-in result [:documents :nodes]))))
      (is (= "example/compute/shared.tfstate" (get-in result [:state_keys :shared])))
      (is (= before (pr-str [opts requirements])))
      (is (= result (planning/plan-deployment opts [{:count 2}] requirements))))))
(deftest single-host-is-explicit-and-key-boundary-removes-private-fields
  (let [[opts _ request] (:args (first fixtures)) requirements (select-keys request [:network :security])
        key (assoc (:key request) :private_key_path "/private/path")]
    (is (= "example-0" (get-in (deployment/deployment-requests opts [{}] requirements key) [:nodes 0 :name])))
    (is (= "example" (get-in (deployment/deployment-requests opts [{}] (assoc requirements :single_host true) key) [:nodes 0 :name])))
    (is (not (contains? (get-in (deployment/deployment-requests opts [{}] requirements key) [:shared :key]) :private_key_path)))
    (is (thrown? Exception (deployment/deployment-requests opts [{:role "db"}] (assoc requirements :single_host true) key)))
    (is (thrown? Exception (planning/plan-deployment opts [{:count 246}] requirements)))))
(deftest public-key-reference-planning-never-opens-file
  (is (= "external" (:mode (key/key-request {:provider-compute "aws" :green/event :build}
                                            {:mode "external" :reference "/does/not/exist.pub"} {}))))
  (is (thrown? Exception (key/key-request {:provider-compute "aws"} {:mode "external" :reference "/does/not/exist.pub"} {})))
  (is (= {:mode "external" :ids [12 "existing-id"] :reference 12}
         (key/key-request {:provider-compute "digitalocean"} {:mode "external" :reference [12 "existing-id"]} {})))
  (is (thrown? Exception (key/key-request {:provider-compute "digitalocean"} {:mode "external" :reference [true]} {}))))
