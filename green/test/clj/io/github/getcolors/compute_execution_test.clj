(ns io.github.getcolors.compute-execution-test
  (:require [clojure.test :refer [deftest is]]
            [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute-execution :as e]
            [io.github.getcolors.compute-request :as request]))
(def fixture (first (json/parse-string-strict (slurp "../test/fixtures/provider-requests.json") true)))
(def documents (:documents (apply request/provider-request (:args fixture))))
(def opts {:profile "example" :provider-compute "aws" :provider-backend "r2" :r2-bucket "states" :r2-endpoint "https://r2.example.test"})
(def environment {"COLORS_PAR_R2_ACCESS_KEY_ID" "r2-access-123456789" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "r2-secret-123456789"
                  "AWS_PROFILE" "ambient-profile" "AWS_ACCESS_KEY_ID" "ambient-access" "AWS_SESSION_TOKEN" "ambient-session" "TF_LOG" "TRACE"})
(def key "example/compute/shared.tfstate")
(defn state-text [provider]
  (json/generate-string {:version 4 :serial 0 :lineage "lineage" :resources [{:type "aws_vpc"}]
                         :outputs {:params {:value {:provider provider} :sensitive false} :network_id {:value "net-example"}}}))
(def valid-plan (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions ["create"]}}]}))
(defn runner [outputs calls]
  (let [responses (atom outputs)]
    (fn [argv cwd env timeout]
      (swap! calls conj {:argv argv :cwd cwd :env env :timeout timeout})
      (is (= "rwx------" (str (java.nio.file.attribute.PosixFilePermissions/toString (java.nio.file.Files/getPosixFilePermissions (.toPath (io/file cwd)) (make-array java.nio.file.LinkOption 0))))))
      (is (every? #(= "rw-------" (java.nio.file.attribute.PosixFilePermissions/toString (java.nio.file.Files/getPosixFilePermissions (.toPath %) (make-array java.nio.file.LinkOption 0)))) (.listFiles (io/file cwd))))
      (let [response (first @responses)] (swap! responses next) (if (map? response) response {:exit 0 :out response :err ""})))))
(deftest guarded-create-orders-commands-and-separates-credentials
  (let [calls (atom []) result (e/converge-state opts key documents "create" {:status "absent"} environment
                                              (runner ["" "" "" valid-plan "" (state-text "aws")] calls))]
    (is (= {:status "ready" :params {:provider "aws"} :outputs {:params {:provider "aws"} :network_id "net-example"}} result))
    (is (= ["init" "state" "plan" "show" "apply" "state"] (mapv #(second (:argv %)) @calls)))
    (is (= [120000 120000 1800000 120000 1800000 120000] (mapv :timeout @calls)))
    (doseq [{:keys [cwd env argv]} @calls]
      (is (not (.exists (io/file cwd))))
      (is (= "ambient-profile" (get env "AWS_PROFILE")))
      (is (= "ambient-access" (get env "AWS_ACCESS_KEY_ID")))
      (is (= "ambient-session" (get env "AWS_SESSION_TOKEN")))
      (is (not (contains? env "TF_LOG")))
      (is (not-any? #(str/starts-with? % "COLORS_PAR_") (keys env)))
      (is (not (str/includes? (pr-str argv) "r2-secret"))))))
(deftest refuses-unknown-state-replacement-and-failed-apply
  (doseq [responses [["" ""] ["" (state-text "azure")]
                     ["" "" "" (json/generate-string {:format_version "1" :planned_values {} :resource_changes [{:change {:actions ["delete" "create"]}}]})]
                     ["" "" "" valid-plan {:exit 1 :out "" :err "sensitive"}]
                     ["" "" "" valid-plan "" (state-text "azure")]]]
    (let [calls (atom []) presence (if (= responses ["" ""]) {:status "present"} {:status "absent"})]
      (is (= {:status "error"} (e/converge-state opts key documents "create" presence environment (runner responses calls))))
      (is (not (.exists (io/file (:cwd (first @calls)))))))))
(deftest delete-and-document-guards-run-no-command
  (let [never (fn [& _] (throw (AssertionError. "must not execute")))]
    (is (= {:status "error"} (e/converge-state opts key documents "delete" {:status "absent"} environment never)))
    (is (= {:status "destroyed"} (e/converge-state (assoc opts :compute-prevent-destroy false) key documents "delete" {:status "absent"} environment never)))
    (is (= {:status "error"} (e/converge-state opts key documents "create" {:status "error"} environment never)))
    (is (= {:status "error"} (e/converge-state opts key {"../bad.tf.json" {}} "create" {:status "absent"} environment never)))))
(deftest exact-presence-and-cancellation-cleanup
  (doseq [[response expected] [[{:exit 1 :err "An error occurred (NoSuchKey) when calling the GetObject operation: absent"} "absent"]
                              [{:exit 1 :err "prefix An error occurred (NoSuchKey) when calling the GetObject operation: absent"} "error"]
                              [{:exit 0 :out "{\"ETag\":\"etag\"}"} "present"]]]
    (is (= {:status expected} (e/state-presence opts key environment (fn [_ _ _ _] response)))))
  (let [directory (atom nil)]
    (is (thrown? InterruptedException (e/converge-state opts key documents "create" {:status "absent"} environment
                                                       (fn [_ cwd _ _] (reset! directory cwd) (throw (InterruptedException. "cancel"))))))
    (is (not (.exists (io/file @directory))))))

(deftest output-credentials-and-sensitive-values-never-return
  (doseq [extra [{:value "r2-secret-123456789"} {:value "private" :sensitive true} {:sensitive false}]]
    (let [calls (atom []) final (assoc-in (json/parse-string (state-text "aws") true) [:outputs :extra] extra)]
      (is (= {:status "error"} (e/converge-state opts key documents "create" {:status "absent"} environment
                                                (runner ["" "" "" valid-plan "" (json/generate-string final)] calls)))))))
(deftest drift-only-accepts-zero-detailed-plan-and-never-applies
  (doseq [exit [0 1 2 -1]]
    (let [calls (atom []) result (e/check-state opts key documents environment (runner ["" (state-text "aws") {:exit exit :out "" :err "sensitive diagnostic"}] calls))]
      (is (= {:status (if (zero? exit) "clean" "error")} result))
      (is (= ["init" "state" "plan"] (mapv #(second (:argv %)) @calls)))
      (is (some #{"-detailed-exitcode"} (:argv (last @calls))))
      (is (every? #(not (.exists (io/file (:cwd %)))) @calls)))))
(deftest selective-vpc-destroy-retry-replans-and-refuses-unclassified-errors
  (doseq [[failures message status attempts] [[1 "Can not delete VPC with members" "destroyed" 2] [9 "Can not delete VPC with members" "error" 4] [1 "unclassified provider failure" "error" 1] [1 "Can not delete VPC with members fixture-do-token" "error" 1]]]
    (let [opts {:profile "demo" :provider-compute "digitalocean" :provider-backend "s3" :s3-bucket "states" :s3-region "us-east-1" :compute-prevent-destroy false}
          docs {"shared.tf.json" {:resource {:digitalocean_vpc {:cluster {:name "demo"}}}}}
          state {:version 4 :serial 1 :lineage "lineage" :resources [{:type "digitalocean_vpc"}] :outputs {:params {:value {:provider "digitalocean"}}}}
          empty (assoc state :resources [] :outputs {}) plan {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions ["delete"]}}]}
          calls (atom []) counter (atom 0) applied (atom false) waits (atom [])
          runner (fn [argv _ _ _] (swap! calls conj (second argv))
                   (case (second argv)
                     "state" {:exit 0 :out (json/generate-string (if @applied empty state)) :err ""}
                     "show" {:exit 0 :out (json/generate-string plan) :err ""}
                     "apply" (if (<= (swap! counter inc) failures) {:exit 1 :out "" :err message} (do (reset! applied true) {:exit 0 :out "" :err ""}))
                     {:exit 0 :out "" :err ""}))]
      (is (= {:status status} (e/converge-state opts "demo/compute/shared.tfstate" docs "delete" {:status "present"} {"COLORS_PAR_DO_TOKEN" "fixture-do-token"} runner #(swap! waits conj %))))
      (is (= attempts @counter)) (is (= (repeat (dec attempts) 30000) @waits)) (is (= attempts (count (filter #{"plan"} @calls)))))))
