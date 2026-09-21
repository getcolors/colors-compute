(ns io.github.getcolors.compute-gcs-test
 (:require [clojure.test :refer :all]
           [io.github.getcolors.compute :as compute]
           [io.github.getcolors.compute-gcs :as gcs]))
(def opts {:provider-backend "gcs" :provider-compute "google" :gcs-bucket "demo-states" :gcs-region "us-central1" :google-project "demo-project" :profile "demo" :gcs-bucket-mode "managed" :compute-prevent-destroy false})
(deftest bounded-generation-read
 (let [calls (atom 0) request (fn [& _] (swap! calls inc) {:generation "1" :size "2097153"})]
  (is (thrown-with-msg? Exception #"too large" (gcs/get-object request "states" "journal" 2097152)))
  (is (= 1 @calls)))
 (is (thrown-with-msg? Exception #"invalid GCS document" (gcs/get-object (fn [_ _ _ query] (if (:alt query) [] {:generation "1" :size "2"})) "states" "journal" 2097152))))
(deftest wire-project-endpoint-and-denial
 (with-redefs [gcs/send-request (fn [_ ^java.net.http.HttpRequest request]
                                 (is (= "https://cloudresourcemanager.googleapis.com/v1/projects/demo-project" (str (.uri request))))
                                 (is (= "Bearer test-token" (.get (.firstValue (.headers request) "Authorization"))))
                                 {:code 403 :body ""})]
  (is (thrown-with-msg? Exception #"403" ((gcs/client {} (fn [& _] {:exit 0 :out "test-token"})) "GET" "v1/projects/demo-project" nil {})))))
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
