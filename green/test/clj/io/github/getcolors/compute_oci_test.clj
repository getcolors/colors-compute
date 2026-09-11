(ns io.github.getcolors.compute-oci-test
 (:require [clojure.test :refer :all] [cheshire.core :as json]
           [io.github.getcolors.compute :as compute] [io.github.getcolors.compute-oci :as oci]
           [io.github.getcolors.compute-journal :as journal] [io.github.getcolors.compute-coordination :as coordination]
           [io.github.getcolors.compute-managed-oci-backend :as backend]
           [io.github.getcolors.compute-coordinator :as coordinator]))
(def opts {:provider-backend "oci" :provider-compute "oci" :oci-bucket "demo-states" :oci-region "eu-frankfurt-1" :oci-namespace "namespace1" :oci-compartment-id "ocid1.compartment.example" :profile "demo" :oci-bucket-mode "managed" :compute-prevent-destroy false})
(deftest rendering
 (let [plan (compute/backend-plan opts "demo/shared.tfstate")]
  (is (= "https://namespace1.compat.objectstorage.eu-frankfurt-1.oraclecloud.com" (get-in plan [:config :terraform :backend :s3 :endpoints :s3])))
  (is (= true (get-in plan [:config :terraform :backend :s3 :use_path_style])))
  (is (= {"COLORS_PAR_OCI_ACCESS_KEY_ID" "access_key" "COLORS_PAR_OCI_SECRET_ACCESS_KEY" "secret_key"} (:credential_bindings plan))))
 (is (some? (coordinator/coordinator opts {} (fn [] {:status "absent"}) nil nil {}))))
(deftest unconfirmed-absence
 (with-redefs [oci/client (fn [& _] (fn [& _] nil))]
  (is (thrown-with-msg? Exception #"absence unconfirmed" (backend/lifecycle opts "bootstrap" {} nil nil)))))
(deftest bootstrap-and-teardown
 (let [bucket (atom nil) marker (atom nil) deleted (atom []) created (atom 0)
       result (fn [data] {:data data :headers {:etag "e1"}})
       request (fn [method path body query headers]
        (cond
         (= method "DELETE") (do (swap! deleted conj [path query]) (result nil))
         (.endsWith path "/b") (if (= method "GET") (result []) (do (swap! created inc) (reset! bucket body) (result body)))
         (.endsWith path "/objectversions") (result {:items [{:name backend/marker-key :versionId "3"} {:name "demo/shared.tfstate" :versionId "1"} {:name "demo/shared.tfstate" :versionId "2"}]})
         (.endsWith path "/o") (result {:objects [{:name "demo/shared.tfstate"}]})
         (.contains path "/o/") (if (= method "PUT") (do (is (or (= "*" (:if-none-match headers)) (= "e1" (:if-match headers)))) (reset! marker body) (result nil))
                                  (if (.contains path "backend-owner") (when @marker (result @marker)) (result {:version 4 :resources []})))
         (= method "PUT") (result (swap! bucket merge body))
         :else (when @bucket (result @bucket))))]
  (with-redefs [oci/client (fn [& _] request) coordinator/acquire! (fn [& _]) coordinator/snapshot (fn [& _] {:document {:status "retired"}}) coordinator/release! (fn [& _])]
   (is (= {:status "ready" :bucket "demo-states"} (backend/lifecycle opts "bootstrap" {} nil nil)))
   (swap! bucket assoc :versioning "Suspended")
   (backend/lifecycle opts "bootstrap" {} nil nil)
   (is (= 1 @created))
   (is (= "Enabled" (:versioning @bucket)))
   (is (= {:status "destroyed"} (backend/lifecycle opts "finalize" {} nil (fn [& _] {}))))
   (is (= 4 (count @deleted)))
   (is (= "/n/namespace1/b/demo-states" (ffirst (reverse @deleted))))
   (is (thrown-with-msg? Exception #"deletion in progress" (backend/lifecycle opts "bootstrap" {} nil nil))))))
(deftest native-journal-precondition
 (let [identity {:profile "demo" :provider "oci" :backend {:kind "oci" :bucket "demo-states" :region "eu-frankfurt-1" :endpoint "https://namespace1.compat.objectstorage.eu-frankfurt-1.oraclecloud.com"}}
       intent (assoc (coordination/coordination {:status "absent"} identity {:type "acquire" :run_id "run" :write_id "first" :target_etag nil}) :condition {:if_match "stale"})
       runner (fn [args _ environment _]
                (is (= ["oci" "raw-request"] (subvec args 0 2)))
                (is (not (contains? environment "COLORS_PAR_OCI_SECRET_ACCESS_KEY")))
                (is (= {:if-match "stale" :content-type "application/json"} (json/parse-string (nth args (inc (.indexOf args "--request-headers"))) true)))
                {:exit 0 :out "{\"status\":\"412 Precondition Failed\"}" :err ""})]
  (is (= {:status "conflict"} (journal/journal-put opts intent {} runner)))))
(deftest recovery-refuses-default-boot-volume-name
 (require 'io.github.getcolors.compute-recovery)
 (doseq [resource-name [nil "Boot volume of instance demo-0"]]
  (let [transitions (atom []) released (atom false)
        doc {:status "active" :key {:phase "prepared"} :shared {:phase "ready"}
             :nodes {:0 {:phase "failed" :operation "create" :operation_id "attempt" :state_key "demo/compute/nodes/0.tfstate"}}}
        runner (fn [args & _] {:exit 0 :out (json/generate-string {:data (if (and resource-name (= "bv" (second args))) [{:display-name resource-name :lifecycle-state "AVAILABLE"}] [])})})]
   (with-redefs [oci/client (fn [& _] (fn [& _] {:data {:version 4 :serial 1 :lineage "line" :outputs {} :resources []} :headers {}}))
                 coordinator/acquire! (fn [& _]) coordinator/snapshot (fn [& _] {:document doc})
                 coordinator/transition! (fn [_ event fields] (swap! transitions conj [event fields]))
                 coordinator/release! (fn [& _] (reset! released true))]
    (let [run #((requiring-resolve 'io.github.getcolors.compute-recovery/recover-absent-oci-nodes!) (assoc opts :oci-availability-domain "ad1") {:0 "attempt"} {} runner (fn [& _] {}))]
     (if resource-name (is (thrown-with-msg? Exception #"absent OCI instances and boot volumes" (run)))
         (is (= {:status "recovered" :nodes ["0"]} (run))))
     (is (= (if resource-name [] [["retry" {:node_id "0" :evidence "verified-provider-absence"}]]) @transitions))
     (is @released))))))
(deftest version-pages-and-partial-marker-purge
 (let [deleted (atom []) marker {:schema 1 :identity {:bucket "demo-states" :region "eu-frankfurt-1" :namespace "namespace1" :compartment "ocid1.compartment.example" :profile "demo"} :status "active"}
       request (fn [method path _ query _]
                 (cond (= method "DELETE") (do (swap! deleted conj (or (:versionId query) "bucket")) {:data nil :headers {}})
                       (.endsWith path "/objectversions") (do (is (not (:start query))) (if (:page query)
                         {:data {:items [{:name backend/marker-key :versionId "2"}]} :headers {}}
                         {:data {:items [{:name "demo/old.tfstate" :versionId "1"}]} :headers {:opc-next-page "next"}}))
                       (.contains path "/o/") {:data marker :headers {:etag "e1"}}
                       :else {:data {:compartmentId "ocid1.compartment.example" :freeformTags {:colors-profile "demo" :colors-purpose "managed-backend" :colors-phase "deleting"}} :headers {:etag "e1"}}))]
  (with-redefs [oci/client (fn [& _] request)]
   (is (= {:status "destroyed"} (backend/lifecycle opts "finalize" {} nil (fn [& _] (throw (ex-info "journal was purged" {}))))))
   (is (= ["1" "2" "bucket"] @deleted)))))
