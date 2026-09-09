(ns io.github.getcolors.compute-test
  (:require [clojure.test :refer [deftest is testing]]
            [clojure.java.io :as io]
            [cheshire.core :as json]
            [io.github.getcolors.compute :as c]))

(def node {:node_id "broker-0" :provider "aws" :name "example-broker-0"
           :ip "192.0.2.1" :user "ubuntu" :sudoer "ubuntu" :metadata {:zone "a"}})
(deftest selection-and-credentials
  (is (= [":provider-compute must be one of aws, azure, digitalocean, google, hcloud, oci, vultr, yandex"
          ":provider-backend must be one of r2, s3" ":profile is required"] (c/validate {})))
  (doseq [provider ["aws" "azure" "google" "oci"]]
    (is (= [] (c/credential-requirements {:provider-compute provider :provider-backend "s3"}))))
  (is (= ["COLORS_PAR_R2_ACCESS_KEY_ID" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "COLORS_PAR_VULTR_API_KEY"]
         (c/credential-requirements {:provider-compute "vultr" :provider-backend "r2"})))
  (is (= [":s3-bucket is required" ":vultr-plan is required"]
         (c/validate {:provider-compute "vultr" :provider-backend "s3" :profile "example"
                      :vultr-plan "replace_ME" :vultr-region "ams" :vultr-os-id 1 :s3-region "eu"}))))
(deftest topology-and-state-identity
  (is (= (c/expand [{:role "broker"}]) (take 1 (c/expand [{:role "broker" :count 3}]))))
  (is (= {:shared "demo/compute/shared.tfstate" :nodes {"0" "demo/compute/nodes/0.tfstate"}}
         (c/state-keys "demo" ["0"])))
  (doseq [count [true 0 -1 1.5 nil]]
    (is (thrown-with-msg? Exception #"count must be a positive integer" (c/expand [{:count count}]))))
  (is (thrown-with-msg? Exception #"invalid role" (c/expand [{:role "../bad"}]))))
(deftest collect-complete-ordered-inventory
  (let [requests (c/expand [{:role "broker" :count 2}])
        other (assoc node :node_id "broker-1" :ip "192.0.2.2")
        joined (c/collect requests [other node] "broker-0")]
    (is (= ["broker-0" "broker-1"] (mapv :node_id (:nodes joined))))
    (is (= {:zone "a"} (-> joined :nodes first :metadata)))
    (is (= "broker" (-> joined :nodes first :role)))
    (is (thrown-with-msg? Exception #"missing node: broker-1" (c/collect requests [node] "broker-0")))
    (is (thrown-with-msg? Exception #"duplicate node: broker-0" (c/collect requests [node node] "broker-0")))
    (is (thrown-with-msg? Exception #"provider mismatch: broker-1" (c/collect requests [node (assoc other :provider "vultr")] "broker-0")))
    (is (thrown-with-msg? Exception #"incomplete node broker-0: vpc_ip"
                          (c/collect [(assoc (first requests) :private true)] [node] "broker-0")))))
(deftest uncertain-state-never-authorizes-mutation
  (is (= {:action "create"} (c/state-decision {:status "absent"} "aws")))
  (is (= {:action "reuse"} (c/state-decision {:status "present" :params {:provider "aws"}} "aws")))
  (is (thrown-with-msg? Exception #"refusing mutation" (c/state-decision {:status "error"} "aws")))
  (is (thrown-with-msg? Exception #"legacy state requires migration" (c/state-decision {:status "present"} "aws"))))
(deftest packaged-registry-does-not-drift
  (is (= c/registry (json/parse-string (slurp (io/file "../contracts/providers.json")) true))))

(deftest malformed-state-and-mixed-provider-refusal
  (doseq [provider [17 true [] {} " " "REPLACE_ME"]]
    (is (thrown-with-msg? Exception #"legacy state requires migration"
                          (c/state-decision {:status "present" :params {:provider provider}} "aws"))))
  (is (thrown-with-msg? Exception #"invalid node_id: null" (c/state-keys "demo" [nil])))
  (let [requests [{:node_id "broker-0" :provider "aws"} {:node_id "broker-1" :provider "vultr"}]]
    (is (thrown-with-msg? Exception #"provider mismatch: broker-1"
                          (c/collect requests [node (assoc node :node_id "broker-1" :provider "vultr")] "broker-0")))))

(deftest structured-template-preserves-json-types
  (let [template {:count "{{count}}" :enabled "{{enabled}}" :empty "{{empty}}"
                  :items ["{{items}}" "${var.image}" 42 false nil]
                  :object "{{object}}" :nested {:value "{{count}}"}}
        inputs {:count 3 :enabled false :empty nil :items [1 "two"] :object {:key "value"}}
        result (c/render-template template inputs)]
    (is (= {:count 3 :enabled false :empty nil :items [[1 "two"] "${var.image}" 42 false nil]
            :object {:key "value"} :nested {:value 3}} result))
    (is (= "{{count}}" (:count template)))
    (is (= inputs {:count 3 :enabled false :empty nil :items [1 "two"] :object {:key "value"}}))
    (is (= {"{{key}}" 1} (c/render-template {"{{key}}" 1} {})))
    (is (thrown-with-msg? Exception #"missing template input: missing" (c/render-template "{{missing}}" {})))
    (doseq [value ["prefix {{count}}" "{{count}} suffix" "{{COUNT}}" "{{count" "count}}"]]
      (is (thrown-with-msg? Exception #"template placeholders must occupy the entire string"
                            (c/render-template value inputs))))))

(deftest backend-planning-does-not-bind-secret-values
  (let [opts {:provider-backend "r2" :r2-bucket "example" :r2-endpoint "https://example.invalid"
              :r2-access-key-id "must-not-appear" :r2-secret-access-key "must-not-appear"}
        plan (c/backend-plan opts "demo/compute/nodes/0.tfstate")]
    (is (= {} (:environment plan)))
    (is (= {"COLORS_PAR_R2_ACCESS_KEY_ID" "access_key" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "secret_key"}
           (:credential_bindings plan)))
    (is (not (clojure.string/includes? (pr-str plan) "must-not-appear")))
    (is (= {:bucket "example" :region "auto" :key "demo/compute/nodes/0.tfstate"
            :use_lockfile true :endpoints {:s3 "https://example.invalid"}
            :use_path_style false :skip_credentials_validation true :skip_metadata_api_check true
            :skip_region_validation true :skip_requesting_account_id true :skip_s3_checksum true}
           (get-in plan [:config :terraform :backend :s3]))))
  (is (= {:config {:terraform {:backend {:s3 {:bucket "example" :region "eu-west-1"
                                            :key "a.tfstate" :use_lockfile true}}}}
          :credential_bindings {} :environment {}}
         (c/backend-plan {:provider-backend "s3" :s3-bucket "example" :s3-region "eu-west-1"} "a.tfstate")))
  (doseq [selection [nil false {} [] "unknown"]]
    (is (thrown-with-msg? Exception #":provider-backend must be one of r2, s3"
                          (c/backend-plan {:provider-backend selection} "../bad"))))
  (is (thrown-with-msg? Exception #":r2-bucket is required"
                        (c/backend-plan {:provider-backend "r2" :r2-bucket "REPLACE_ME"} "../bad")))
  (doseq [key [nil true "" "a/" "/a" "a//b" "../a" "a/./b" "a b" "a\nb"]]
    (is (thrown-with-msg? Exception #"invalid state key"
                          (c/backend-plan {:provider-backend "s3" :s3-bucket "example" :s3-region "eu"} key)))))
