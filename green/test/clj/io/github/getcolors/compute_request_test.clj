(ns io.github.getcolors.compute-request-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [io.github.getcolors.compute-request :as r]))
(deftest shared-provider-request-fixtures
  (doseq [{:keys [name args expected]} (json/parse-string (slurp "../test/fixtures/provider-requests.json") true)]
    (testing name
      (let [before (pr-str args)
            result (try (apply r/provider-request args) (catch Exception e {:error (.getMessage e)}))]
        (is (= expected (json/parse-string (json/generate-string result) true)))
        (is (= before (pr-str args)))))))

(deftest malformed-and-template-injection-requests-refused
  (let [[opts stage request shared] (:args (first (json/parse-string (slurp "../test/fixtures/provider-requests.json") true)))]
    (doseq [[changed expected]
            [[(assoc request :unexpected true) "invalid compute request"]
             [(assoc-in request [:security :ingress 0 :from_port] true) "invalid compute ingress rule"]
             [(assoc-in request [:network :cidr] "10.42.0.1/16") "invalid compute CIDR"]]]
      (is (thrown? Exception (r/provider-request opts stage changed shared))))
    (is (thrown? Exception (r/provider-request (assoc opts :aws-region (str "$" "{danger}")) stage request shared)))
    (is (thrown? Exception (r/provider-request opts "node" request {:params {:provider "azure"}})))))

(deftest icmp-provider-fixtures
  (doseq [{:keys [name args expected]} (json/parse-string (slurp "../test/fixtures/provider-icmp.json") true)]
    (testing name
      (is (= expected (json/parse-string (json/generate-string (try (apply r/provider-request args) (catch Exception e {:error (.getMessage e)}))) true))))))

(deftest endpoint-provider-fixtures
  (doseq [{:keys [name args expected]} (json/parse-string (slurp "../test/fixtures/provider-endpoint.json") true)]
    (testing name
      (is (= expected (json/parse-string (json/generate-string (try (apply r/provider-request args) (catch Exception e {:error (.getMessage e)}))) true))))))
