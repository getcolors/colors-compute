(ns io.github.getcolors.compute-request-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [io.github.getcolors.compute-request :as r]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-planning :as planning]))
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

(deftest role-provider-fixtures
  (doseq [{:keys [name args expected]} (json/parse-string (slurp "../test/fixtures/provider-roles.json") true)]
    (testing name
      (let [before (pr-str args)
            actual (try (apply r/provider-request args) (catch Exception e {:error (.getMessage e)}))]
        (is (= expected (json/parse-string (json/generate-string actual) true)))
        (is (= before (pr-str args)))))))

(deftest role-assembly-planning-and-settings-are-pure
  (let [[opts _ request] (:args (first (json/parse-string (slurp "../test/fixtures/provider-roles.json") true)))
        topology [{:role "db" :count 1} {:role "app" :count 1}]
        requirements {:security (:security request) :network (:network request) :roles (:roles request) :entry_node_id "app-0"}
        before (pr-str [opts topology requirements])
        assembly (deployment/deployment-requests opts topology requirements (:key request))
        result (planning/plan-deployment opts topology requirements)]
    (is (= ["db" "app"] (mapv :role (:nodes assembly))))
    (is (= "app-0" (:entry_node_id assembly)))
    (is (= "app-0" (get-in result [:cluster :entry_node_id])))
    (is (= "10.42.1.11" (get-in result [:documents :shared "shared-roles.tf.json" "locals" "ingress" "db:db:peer:app-0" :subnet])))
    (is (= before (pr-str [opts topology requirements])))
    (doseq [bad [(assoc requirements :entry_node_id "missing")
                 (assoc requirements :roles {:db (get-in requirements [:roles :db])})
                 (assoc-in requirements [:roles :db :security :ingress 1 :peer_roles] ["unknown"])]]
      (is (thrown? Exception (deployment/deployment-requests opts topology bad (:key request)))))
    (is (thrown? Exception (deployment/deployment-requests (assoc opts :compute-role-settings {:missing {:size "a"}}) topology requirements (:key request))))))

(deftest vultr-ipv6-rules-retain-canonical-family
  (let [[opts _ request] (:args (first (json/parse-string (slurp "../test/fixtures/provider-roles.json") true)))
        request (-> request (dissoc :roles) (assoc-in [:security :ingress 0 :sources] ["2606:4700::/32"]))
        result (r/provider-request opts "shared" request)]
    (is (= "v6" (get-in result [:inputs :ingress "ssh:2606:4700::/32" :ip_type])))
    (is (thrown? Exception (r/provider-request opts "shared" (assoc-in request [:security :ingress 0 :sources] ["2606:4700:0:0::/32"]))))))

(deftest oci-private-ingress-uses-discovered-subnet
  (let [[opts stage request shared] (:args (first (filter #(= "oci-shared" (:name %))
                                                (json/parse-string (slurp "../test/fixtures/provider-requests.json") true))))
        request (-> request (assoc :network {:mode "discovered"}) (assoc-in [:security :private_filter] true))
        result (r/provider-request opts stage request shared)]
    (is (= "private" (get-in result [:inputs :rules "peer:private" :cidr])))
    (is (= 9093 (get-in result [:inputs :rules "peer:private" :from_port])))
    (is (= "192.0.2.1/32" (get-in result [:inputs :rules "ssh:192.0.2.1/32" :cidr])))))
