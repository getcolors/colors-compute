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
