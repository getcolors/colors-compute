(ns io.github.getcolors.compute-recovery-test
  (:require [clojure.test :refer [deftest is]]
            [io.github.getcolors.compute-recovery :as recovery]
            [io.github.getcolors.compute-coordinator :as c]
            [io.github.getcolors.compute-coordinator-test :as ct]))
(def opts (assoc ct/opts :aws-region "us-east-1"))
(defn failed-world []
  (let [storage (ct/store) ids (ct/ids) factory (fn [opts env] (c/coordinator opts env (:read storage) (:write storage) ids {:event-prefix "lifecycle/"}))
        owner (factory opts {})]
    (c/acquire! owner)
    (c/declare! owner [{:role nil :count 1}])
    (c/transition! owner "key-intent" {:mode "managed"})
    (c/transition! owner "key-prepared" {:fingerprint (str "SHA256:" (apply str (repeat 43 "A")))})
    (let [id (c/transition! owner "shared-start" {})]
      (c/transition! owner "shared-fail" {:operation_id id})
      (c/release! owner)
      {:factory factory :storage storage :id id})))
(defn runner [count calls]
  (fn [args _ _ _]
    (swap! calls conj args)
    (if (= "s3api" (second args)) {:exit 1 :out "" :err "An error occurred (NoSuchKey) when calling the GetObject operation: missing"}
        {:exit 0 :out (str count) :err ""})))
(deftest recovery-records-verified-absence-through-real-journal
  (let [{:keys [factory storage id]} (failed-world) calls (atom [])]
    (is (= {:status "recovered"} (recovery/recover-absent-aws-shared! opts id {} (runner 0 calls) factory)))
    (is (= 7 (count @calls)))
    (is (= "declared" (get-in @(:state storage) [:observation :document :shared :phase])))
    (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))))
(deftest recovery-refuses-surviving-resources
  (let [{:keys [factory storage id]} (failed-world) calls (atom [])]
    (is (thrown? Exception (recovery/recover-absent-aws-shared! opts id {} (runner 1 calls) factory)))
    (is (= "failed" (get-in @(:state storage) [:observation :document :shared :phase])))
    (is (= "idle" (get-in @(:state storage) [:observation :document :lock :state])))))

(def repair-identity {:profile "demo" :provider "vultr" :backend {:kind "s3" :bucket "states" :region "eu-west-1"}})
(def repair-opts {:profile "demo" :provider-compute "vultr" :provider-backend "s3" :s3-bucket "states" :s3-region "eu-west-1"})
(defn interrupted-node [i phase op oid]
  {:state_key (str "demo/compute/nodes/" i ".tfstate") :role nil :index i :desired true :phase phase :operation op :operation_id oid})
(def interrupted-doc
  {:schema_version 2 :identity repair-identity :revision 7 :write_id "write-7" :lock {:state "held" :run_id "run-dead"} :generation 1
   :status "active" :topology_declared true :key {:mode "managed" :phase "prepared" :fingerprint (str "SHA256:" (apply str (repeat 43 "A")))}
   :shared {:phase "ready" :operation "create" :operation_id "op-shared"}
   :nodes {:0 (interrupted-node 0 "running" "create" "op-0")}})
(def observed {:status "present" :etag "etag-7" :document interrupted-doc})
(def declared {:nodes {:0 (interrupted-node 0 "declared" nil nil)}})
(defn journal [reads]
  (let [reads (atom reads) puts (atom [])]
    {:puts puts
     :deps {:write-id "write-8"
            :journal-get (fn [& _] (let [[r & more] @reads] (reset! reads (or more [r])) r))
            :journal-put (fn [_ intent _] (swap! puts conj intent) {:status "written" :etag "etag-8"})}}))
(deftest reviewed-repair-commits-with-precondition-and-read-back
  (let [expected (assoc interrupted-doc :nodes (:nodes declared) :lock {:state "idle" :run_id nil} :revision 8 :write_id "write-8")
        {:keys [puts deps]} (journal [observed {:status "present" :etag "etag-8" :document expected}])]
    (is (= {:status "written" :etag "etag-8"} (recovery/commit-reviewed-repair! repair-opts {} observed "run-dead" declared deps)))
    (is (= [{:condition {:if_match "etag-7"} :document expected}] @puts))))
(deftest reviewed-repair-refuses-stale-or-unconfirmed-writes
  (let [{:keys [puts deps]} (journal [(assoc observed :etag "etag-moved")])]
    (is (thrown-with-msg? Exception #"lifecycle stale observation" (recovery/commit-reviewed-repair! repair-opts {} observed "run-dead" declared deps)))
    (is (empty? @puts)))
  (let [{:keys [puts deps]} (journal [observed observed])]
    (is (thrown-with-msg? Exception #"repair not confirmed" (recovery/commit-reviewed-repair! repair-opts {} observed "run-dead" declared deps)))
    (is (= 1 (count @puts))))
  (let [{:keys [puts deps]} (journal [observed])]
    (is (thrown-with-msg? Exception #"lifecycle owner mismatch" (recovery/commit-reviewed-repair! repair-opts {} observed "run-other" declared deps)))
    (is (empty? @puts))))
