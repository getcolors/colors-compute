(ns io.github.getcolors.compute-drift-test
 (:require [clojure.test :refer [deftest is]] [cheshire.core :as json]
           [io.github.getcolors.compute-drift :as drift]
           [io.github.getcolors.compute-orchestration :as orchestration]
           [io.github.getcolors.compute-orchestration-test :as ot]))
(deftest owned-state-drift-under-lease
 (let [{:keys [deps storage states]} (ot/world)
       fixture (first (filter #(and (= "aws" (get-in % [:args 0 :provider-compute])) (= "shared" (get-in % [:args 1]))) (json/parse-string-strict (slurp "../test/fixtures/provider-requests.json") true)))
       [base _ request] (:args fixture) opts (merge base ot/opts)
       requirements (select-keys request [:security :network]) checks (atom [])]
  (is (= "ready" (:status (orchestration/orchestrate opts ot/topology ot/requirements {} deps))))
  (swap! states update-in ["demo/compute/shared.tfstate" :outputs] merge {:ssh_key_id "key-id" :key_name "key-name" :params {:provider "aws" :vpc_id "vpc" :subnet_id "subnet" :security_group_id "sg"}})
  (let [before (get-in @(:state storage) [:observation :document])
        drift-deps {:journal-get (fn [& _] ((:read storage))) :journal-put (fn [_ intent _] ((:write storage) intent)) :read-state (:read-state deps)
                    :public-key (fn [& _] "ssh-ed25519 fixture") :compute-credential-errors (fn [& _] [])
                    :check-state (fn [_ key _ _] (is (= "held" (get-in @(:state storage) [:observation :document :lock :state]))) (swap! checks conj key) {:status "clean"})}]
   (is (= {:status "clean"} (drift/check-deployment-drift opts ot/topology requirements {} drift-deps)))
   (is (= ["demo/compute/shared.tfstate" "demo/compute/nodes/broker-0.tfstate" "demo/compute/nodes/broker-1.tfstate"] @checks))
   (is (= (select-keys before [:nodes :shared :key :generation :status :topology_declared]) (select-keys (get-in @(:state storage) [:observation :document]) [:nodes :shared :key :generation :status :topology_declared])))
   (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))
   (is (= {:status "error"} (drift/check-deployment-drift opts ot/topology (assoc requirements :private true) {} drift-deps)))
   (is (= 3 (count @checks)))
   (is (= {:status "error"} (drift/check-deployment-drift opts ot/topology requirements {} (assoc drift-deps :check-state (fn [& _] {:status "error"})))))
   (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state]))))))
(deftest public-key-read-never-opens-or-creates-private-file
  (let [home (java.nio.file.Files/createTempDirectory "drift-public-" (make-array java.nio.file.attribute.FileAttribute 0))
        dir (.resolve home ".ssh") public (.resolve dir "demo.pub")
        value "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA comment"]
    (try
      (java.nio.file.Files/createDirectory dir (make-array java.nio.file.attribute.FileAttribute 0))
      (spit (str public) value)
      (let [before (java.nio.file.Files/getPosixFilePermissions public (make-array java.nio.file.LinkOption 0))
            fingerprint (#'io.github.getcolors.compute-ssh/fingerprint value)]
        (is (= value (#'drift/public-key {:profile "demo"} fingerprint {"HOME" (str home)})))
        (is (not (.exists (clojure.java.io/file (str (.resolve dir "demo"))))))
        (is (= before (java.nio.file.Files/getPosixFilePermissions public (make-array java.nio.file.LinkOption 0))))
        (is (thrown? Exception (#'drift/public-key {:profile "demo"} (str "SHA256:" (apply str (repeat 43 "A"))) {"HOME" (str home)}))))
      (finally (doseq [file (reverse (file-seq (clojure.java.io/file (str home))))] (.delete file))))))
