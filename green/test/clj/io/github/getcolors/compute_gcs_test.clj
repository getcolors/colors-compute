(ns io.github.getcolors.compute-gcs-test
 (:require [clojure.test :refer :all]
           [io.github.getcolors.compute :as compute]
           [io.github.getcolors.compute-gcs :as gcs]
           [io.github.getcolors.compute-managed]
           [io.github.getcolors.compute-managed-gcs-backend :as backend]
           [io.github.getcolors.compute-coordinator :as coordinator]))
(def opts {:provider-backend "gcs" :provider-compute "google" :gcs-bucket "demo-states" :gcs-region "us-central1" :google-project "demo-project" :profile "demo" :gcs-bucket-mode "managed" :compute-prevent-destroy false})
(deftest rendering
 (is (= {:gcs {:bucket "demo-states" :prefix "demo/shared.tfstate"}} (get-in (compute/backend-plan opts "demo/shared.tfstate") [:config :terraform :backend])))
 (is (some? (coordinator/coordinator opts {} (fn [] {:status "absent"}) nil nil {}))))
(deftest ownership
 (with-redefs [gcs/client (fn [& _] (fn [_ path & _] (if (.startsWith path "v1/projects/") {:projectId "demo-project" :projectNumber "123456789"} {:labels {} :location "US-CENTRAL1" :projectNumber "123456789"})))]
  (is (thrown-with-msg? Exception #"ownership mismatch" (backend/lifecycle opts "bootstrap" {} nil nil)))))
(deftest bootstrap-and-teardown
 (let [bucket (atom nil) marker (atom nil) deleted (atom []) created (atom 0)
       request (fn [method path body query]
        (cond
         (.startsWith path "v1/projects/") {:projectId "demo-project" :projectNumber "123456789"}
         (= method "DELETE") (do (swap! deleted conj [path query]) {})
         (= method "PATCH") (swap! bucket merge body)
         (= path "storage/v1/b") (do (swap! created inc) (reset! bucket (assoc body :metageneration "1" :projectNumber "123456789")))
         (.startsWith path "upload/") (do (reset! marker body) {:generation "3"})
         (.endsWith path "/o") {:items (if (:versions query) [{:name "demo/shared.tfstate/default.tfstate" :generation "1"} {:name "demo/shared.tfstate/default.tfstate" :generation "2"} {:name backend/marker-key :generation "3"}] [{:name "demo/shared.tfstate/default.tfstate"}])}
         (.contains path "/o/") (if (= "media" (:alt query)) (if (.contains path "backend-owner") @marker {:version 4 :resources []}) (when @marker {:generation "1"}))
         :else @bucket))]
  (with-redefs [gcs/client (fn [& _] request) coordinator/acquire! (fn [& _]) coordinator/snapshot (fn [& _] {:document {:status "retired"}}) coordinator/release! (fn [& _])]
   (is (= {:status "ready" :bucket "demo-states"} (backend/lifecycle opts "bootstrap" {} nil nil)))
   (is (= {:status "ready" :bucket "demo-states"} (backend/lifecycle opts "bootstrap" {} nil nil)))
   (swap! bucket assoc :versioning {:enabled false})
   (backend/lifecycle opts "bootstrap" {} nil nil)
   (is (= 1 @created))
   (is (= true (get-in @bucket [:versioning :enabled])))
   (is (= {:status "destroyed"} (backend/lifecycle opts "finalize" {} nil (fn [& _] {}))))
   (is (= 4 (count @deleted)))
   (is (= "storage/v1/b/demo-states" (ffirst (reverse @deleted)))))))
(deftest state-presence
 (require 'io.github.getcolors.compute-execution)
 (with-redefs [gcs/client (fn [& _] (fn [method path body query]
                                   (is (= "storage/v1/b/demo-states/o/demo%2Fcompute%2Fshared.tfstate%2Fdefault.tfstate" path))
                                   {:generation "22"}))]
  (is (= {:status "present"} ((requiring-resolve 'io.github.getcolors.compute-execution/state-presence) opts "demo/compute/shared.tfstate" {} nil)))))
(deftest bounded-generation-read
 (let [calls (atom 0) request (fn [& _] (swap! calls inc) {:generation "1" :size "2097153"})]
  (is (thrown-with-msg? Exception #"too large" (gcs/get-object request "states" "journal" 2097152)))
  (is (= 1 @calls)))
 (is (thrown-with-msg? Exception #"invalid GCS document" (gcs/get-object (fn [_ _ _ query] (if (:alt query) [] {:generation "1" :size "2"})) "states" "journal" 2097152))))
(deftest managed-identity-includes-region
 (is (= "us-central1" (get-in ((deref (ns-resolve 'io.github.getcolors.compute-managed 'identity)) opts) [:backend :region]))))
(deftest project-ownership
 (doseq [action ["bootstrap" "finalize"] project-number ["987654321" nil 123456789]]
  (let [calls (atom [])]
   (with-redefs [gcs/client (fn [& _] (fn [method path & _]
                                      (swap! calls conj method)
                                      (if (.startsWith path "v1/projects/") {:projectId "demo-project" :projectNumber "123456789"}
                                       {:projectNumber project-number :location "US-CENTRAL1"
                                        :labels {:colors_profile "demo" :colors_project "demo-project" :colors_purpose "managed-backend"}})))]
    (is (thrown-with-msg? Exception #"ownership mismatch" (backend/lifecycle opts action {} nil nil)))
    (is (= ["GET" "GET"] @calls)))))
 (doseq [resolved [nil {} {:projectId "foreign-project" :projectNumber "123456789"}
                       {:projectId "demo-project" :projectNumber 123456789}
                       {:projectId "demo-project" :projectNumber "1.2"}
                       {:projectId "demo-project" :projectNumber "0"}]]
  (let [calls (atom [])]
   (with-redefs [gcs/client (fn [& _] (fn [method path & _] (swap! calls conj [method path]) resolved))]
    (is (thrown-with-msg? Exception #"project verification failed" (backend/lifecycle opts "bootstrap" {} nil nil)))
    (is (= [["GET" "v1/projects/demo-project"]] @calls))))))
(deftest wire-project-endpoint-and-denial
 (with-redefs [gcs/send-request (fn [_ ^java.net.http.HttpRequest request]
                                 (is (= "https://cloudresourcemanager.googleapis.com/v1/projects/demo-project" (str (.uri request))))
                                 (is (= "Bearer test-token" (.get (.firstValue (.headers request) "Authorization"))))
                                 {:code 403 :body ""})]
  (is (thrown-with-msg? Exception #"403" (backend/lifecycle opts "bootstrap" {} (fn [& _] {:exit 0 :out "test-token"}) nil)))))
(deftest wire-object-absence
 (doseq [bucket-response [{:code 200 :body "{\"name\":\"demo-states\"}"} {:code 404 :body ""} {:code 403 :body ""}
                         {:code 412 :body ""} {:code 200 :body "{}"} {:code 200 :body "{\"name\":\"foreign\"}"}]]
  (let [calls (atom [])]
   (with-redefs [gcs/send-request (fn [_ ^java.net.http.HttpRequest request]
                                  (let [path (.getPath (.uri request))]
                                   (swap! calls conj path)
                                   (if (.contains path "/o/") {:code 404 :body ""} bucket-response)))]
    (let [request (gcs/client {} (fn [& _] {:exit 0 :out "test-token"}))]
     (if (= "{\"name\":\"demo-states\"}" (:body bucket-response))
      (is (nil? (request "GET" "storage/v1/b/demo-states/o/missing" nil {})))
      (is (thrown? Exception (request "GET" "storage/v1/b/demo-states/o/missing" nil {}))))
     (is (= ["/storage/v1/b/demo-states/o/missing" "/storage/v1/b/demo-states"] @calls))
     (when (= 404 (:code bucket-response))
      (is (nil? (request "GET" "storage/v1/b/demo-states" nil {})))))))))
(deftest created-project-before-marker
 (let [calls (atom [])]
  (with-redefs [gcs/client (fn [& _] (fn [method path body _]
                                     (swap! calls conj [method path])
                                     (cond (.startsWith path "v1/projects/") {:projectId "demo-project" :projectNumber "123456789"}
                                           (= method "GET") nil
                                           :else (assoc body :projectNumber "987654321"))))]
   (is (thrown-with-msg? Exception #"ownership mismatch" (backend/lifecycle opts "bootstrap" {} nil nil)))
   (is (= [["GET" "v1/projects/demo-project"] ["GET" "storage/v1/b/demo-states"] ["POST" "storage/v1/b"]] @calls)))))
