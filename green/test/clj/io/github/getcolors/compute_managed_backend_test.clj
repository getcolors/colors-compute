(ns io.github.getcolors.compute-managed-backend-test
  (:require [clojure.test :refer [deftest is testing]] [cheshire.core :as json]
            [io.github.getcolors.compute-managed-backend :as backend]
            [io.github.getcolors.compute-coordinator :as coordinator]))
(def opts {:provider-backend "s3" :s3-bucket-mode "managed" :s3-bucket "demo-state-123456789012-us-east-1" :s3-region "us-east-1" :profile "demo" :compute-prevent-destroy false})
(def identity {:account "123456789012" :bucket (:s3-bucket opts) :region "us-east-1" :profile "demo"})
(defn fake [exists?]
  (let [state (atom {:exists exists? :calls [] :marker {:schema 1 :identity identity :status "active"} :objects {}})]
    {:state state
     :runner (fn [args _ _ _]
       (let [op (nth args 2) value-after #(nth args (inc (.indexOf args %)))
             _ (swap! state update :calls conj op)
             result (case op
                      "get-caller-identity" {:Account "123456789012"}
                      "head-bucket" (if (:exists @state) {} ::missing)
                      "create-bucket" (do (swap! state assoc :exists true) {})
                      "get-bucket-tagging" {:TagSet (cond-> [{:Key "colors:profile" :Value "demo"} {:Key "colors:owner" :Value "123456789012"} {:Key "colors:purpose" :Value "managed-backend"}]
                                                     (:phase @state) (conj {:Key "colors:phase" :Value (:phase @state)}))}
                      "put-bucket-tagging" (do (swap! state assoc :phase (some #(when (= "colors:phase" (:Key %)) (:Value %)) (:TagSet (json/parse-string (value-after "--tagging") true)))) {})
                      "get-object" (let [key (value-after "--key") document (if (= key backend/marker-key) (:marker @state) (get-in @state [:objects key]))]
                                     (if (nil? document) ::missing-key
                                       (do (spit (nth args (+ 2 (.indexOf args "--key"))) (json/generate-string document)) {:ETag "etag"})))
                      "put-object" (do (swap! state assoc :marker (json/parse-string (slurp (value-after "--body")) true)) {})
                      "list-objects-v2" {:Contents (mapv (fn [key] {:Key key}) (keys (:objects @state)))}
                      {})]
         (case result
           ::missing {:exit 1 :out "" :err "An error occurred (404) when calling the HeadBucket operation: missing"}
           ::missing-key {:exit 1 :out "" :err "An error occurred (NoSuchKey) when calling the GetObject operation: missing"}
           {:exit 0 :out (json/generate-string result) :err ""})))}))
(deftest bootstrap-owned-bucket
  (let [{:keys [state runner]} (fake false)]
    (is (= "ready" (:status (backend/bootstrap-backend! opts {} runner))))
    (is (= "ready" (:status (backend/bootstrap-backend! opts {} runner))))
    (is (= 1 (count (filter #{"create-bucket"} (:calls @state)))))
    (doseq [op ["put-public-access-block" "put-bucket-encryption" "put-bucket-versioning"]] (is (some #{op} (:calls @state))))))
(deftest external-and-build-no-io
  (let [{:keys [state runner]} (fake false)]
    (is (= {:status "skipped"} (backend/bootstrap-backend! (assoc opts :s3-bucket-mode "external") {} runner)))
    (is (= {:status "skipped"} (backend/bootstrap-backend! (assoc opts :green/event :build) {} runner)))
    (is (empty? (:calls @state)))))
(deftest finalize-refuses-live-state
  (let [{:keys [state runner]} (fake true) released (atom false)]
    (swap! state assoc-in [:objects "demo/dns.tfstate"] {:version 4 :resources [{:instances [{}]}]})
    (with-redefs [coordinator/acquire! (fn [_] nil) coordinator/snapshot (fn [_] {:document {:status "retired"}}) coordinator/release! (fn [_] (reset! released true))]
      (is (thrown? Exception (backend/finalize-backend! opts {} runner (fn [_ _] {})))))
    (is @released)
    (is (not-any? #{"delete-bucket" "delete-objects"} (:calls @state)))))
(deftest resume-marker-purge
  (let [{:keys [state runner]} (fake true)]
    (swap! state assoc :marker nil :phase "deleting")
    (is (thrown? Exception (backend/bootstrap-backend! opts {} runner)))
    (is (= {:status "destroyed"} (backend/finalize-backend! opts {} runner)))))
(deftest presence-is-read-only
  (let [{:keys [state runner]} (fake false)]
    (is (= {:status "absent"} (backend/backend-presence opts {} runner)))
    (is (= ["get-caller-identity" "head-bucket"] (:calls @state)))
    (is (= {:status "skipped"} (backend/backend-presence (assoc opts :s3-bucket-mode "external") {} runner)))
    (is (= {:status "skipped"} (backend/backend-presence (assoc opts :green/dry-run true) {} runner))))
  (let [{:keys [state runner]} (fake true)]
    (is (= {:status "present"} (backend/backend-presence opts {} runner)))
    (is (= ["get-caller-identity" "head-bucket"] (:calls @state)))))
