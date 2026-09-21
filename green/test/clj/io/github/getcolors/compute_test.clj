(ns io.github.getcolors.compute-test
  (:require [clojure.test :refer [deftest is testing]]
            [clojure.java.io :as io]
            [cheshire.core :as json]
            [io.github.getcolors.compute :as c]))

(def node {:node_id "broker-0" :provider "aws" :name "example-broker-0"
           :ip "192.0.2.1" :user "ubuntu" :sudoer "ubuntu" :metadata {:zone "a"}})
(deftest selection-and-credentials
  (is (= [":provider-compute must be one of aws, azure, digitalocean, google, hcloud, oci, vultr, yandex"
          ":provider-backend must be one of gcs, local, oci, r2, s3" ":profile is required"] (c/validate {})))
  (doseq [provider ["aws" "azure" "google" "oci"]]
    (is (= [] (c/credential-requirements {:provider-compute provider :provider-backend "s3"}))))
  (is (= ["COLORS_PAR_R2_ACCESS_KEY_ID" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "COLORS_PAR_VULTR_API_KEY"]
         (c/credential-requirements {:provider-compute "vultr" :provider-backend "r2"})))
  (is (= [":s3-bucket is required" ":vultr-plan is required"]
         (c/validate {:provider-compute "vultr" :provider-backend "s3" :profile "example"
                      :vultr-plan "replace_ME" :vultr-region "ams" :vultr-os-id 1 :s3-region "eu"}))))
(deftest uncertain-state-never-authorizes-mutation
  (is (= {:action "create"} (c/state-decision {:status "absent"} "aws")))
  (is (= {:action "reuse"} (c/state-decision {:status "present" :params {:provider "aws"}} "aws")))
  (is (thrown-with-msg? Exception #"refusing mutation" (c/state-decision {:status "error"} "aws")))
  (is (thrown-with-msg? Exception #"legacy state requires migration" (c/state-decision {:status "present"} "aws"))))
(deftest packaged-registry-does-not-drift
  (is (= c/registry (json/parse-string (slurp (io/file "../contracts/providers.json")) true))))

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
    (is (thrown-with-msg? Exception #":provider-backend must be one of gcs, local, oci, r2, s3"
                          (c/backend-plan {:provider-backend selection} "../bad"))))
  (is (thrown-with-msg? Exception #":r2-bucket is required"
                        (c/backend-plan {:provider-backend "r2" :r2-bucket "REPLACE_ME"} "../bad")))
  (doseq [key [nil true "" "a/" "/a" "a//b" "../a" "a/./b" "a b" "a\nb"]]
    (is (thrown-with-msg? Exception #"invalid state key"
                          (c/backend-plan {:provider-backend "s3" :s3-bucket "example" :s3-region "eu"} key)))))

(deftest packaged-provider-plan
  (let [inputs (json/parse-string (slurp (io/file "../providers/vultr/examples/inputs.json")) true)
        plan (c/provider-plan "vultr" "node" inputs)
        expected (json/parse-string (slurp (io/file "../providers/vultr/examples/node/main.tf.json")))]
    (is (= {"node.tf.json" expected} plan))
    (is (= "${vultr_instance.node.main_ip}"
           (get-in plan ["node.tf.json" "output" "params" "value" "ip"])))
    (is (true? (get-in plan ["node.tf.json" "resource" "vultr_instance" "node" "lifecycle" "prevent_destroy"])))
    (is (= {"shared-keygen.tf.json"
            (json/parse-string (slurp (io/file "../providers/vultr/examples/shared-keygen/key.tf.json")))
            "shared.tf.json"
            (json/parse-string (slurp (io/file "../providers/vultr/examples/shared-keygen/main.tf.json")))}
           (json/parse-string (json/generate-string (c/provider-plan "vultr" "shared-keygen" inputs))))))
  (is (thrown-with-msg? Exception #"^compute provider templates unavailable: unknown$"
                        (c/provider-plan "unknown" "unknown" {})))
  (is (thrown-with-msg? Exception #"^unsupported compute stage: absent$"
                        (c/provider-plan "vultr" "absent" {})))
  (is (thrown-with-msg? Exception #"^missing template input: "
                        (c/provider-plan "vultr" "node" {}))))
