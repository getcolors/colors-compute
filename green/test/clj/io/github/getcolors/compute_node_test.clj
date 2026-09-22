(ns io.github.getcolors.compute-node-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [clojure.string :as str]
            [io.github.getcolors.compute-node :as node]
            [io.github.getcolors.compute-local :as local])
  (:import [java.nio.file Files Path]))
(def fixtures (json/parse-string (slurp "../test/fixtures/provider-requests.json") true))
(def identity (json/parse-string (slurp "../test/fixtures/public-ssh.json") true))
(def registrations {"aws" :aws_key_pair "digitalocean" :digitalocean_ssh_key "hcloud" :hcloud_ssh_key "vultr" :vultr_ssh_key})
(defn inputs [workdir provider]
  (let [[opts _ request] (:args (first (filter #(and (= provider (get-in % [:args 0 :provider-compute])) (= "shared" (get-in % [:args 1]))) fixtures)))]
    [(assoc opts :provider-backend "local")
     (cond-> (assoc (select-keys request [:security :network]) :node_id "app-0" :workdir workdir :state_filename "app-0.tfstate" :ssh_resource identity)
       (contains? registrations provider) (assoc :ssh_registration {:status "ready" :reference "registration/fixture" :id "12345" :provider provider :ssh_resource_reference (:reference identity) :fingerprint (:fingerprint identity)}))]))
(defn temp-dir [] (str (.toRealPath (Files/createTempDirectory "compute-node-test-" (local/attrs "rwx------")) (make-array java.nio.file.LinkOption 0))))
(defn remove-tree [root]
  (with-open [paths (Files/walk (local/path root) (make-array java.nio.file.FileVisitOption 0))]
    (doseq [p (reverse (sort-by #(.getNameCount ^Path %) (iterator-seq (.iterator paths))))] (Files/deleteIfExists p))))
(deftest public-only-provider-roots
  (doseq [provider ["aws" "azure" "digitalocean" "google" "hcloud" "oci" "vultr" "yandex"] backend ["local" "r2"]]
    (let [[opts request] (inputs "/tmp/sdk" provider)
          plan (node/node-plan (assoc opts :provider-backend backend :r2-bucket "fixture" :r2-endpoint "https://example.r2.cloudflarestorage.com" :s3-prefix "infra") request)
          root (get-in plan [:documents "compute.tf.json"]) text (json/generate-string root)]
      (is (= {} (:key_objects plan)))
      (is (= (str "infra/" (:profile opts) "/app-0.tfstate") (:state_key plan)))
      (is (= (:reference identity) (get-in root [:output :compute_identity :value :ssh_resource_reference])))
      (doseq [token ["tls_private_key" "aws_s3_object" "private_key" "keys_access_key" "sentinel"]] (is (not (str/includes? text token))))
      (doseq [kind (vals registrations)] (is (not (contains? (:resource root) kind)))))))
(deftest separate-public-registration
  (doseq [[provider kind] registrations]
    (let [[opts _] (inputs "/tmp/sdk" provider)
          plan (node/registration-plan opts {:name "access" :workdir "/tmp/sdk" :state_filename "registration.tfstate" :ssh_resource identity})
          root (get-in plan [:documents "compute.tf.json"])]
      (is (= #{kind} (set (keys (:resource root)))))
      (is (= "ssh-registration" (get-in root [:output :compute_identity :value :kind])))
      (is (nil? (:data root)))
      (is (str/includes? (json/generate-string root) (:public_key identity))))))
(deftest identity-and-registration-validation
  (let [[opts request] (inputs "/tmp/sdk" "digitalocean")]
    (doseq [change [{:ssh_resource {}} {:ssh_resource (assoc identity :fingerprint "SHA256:wrong")} {:node_id "../bad"} {:workdir "relative"} {:state_filename "../bad.tfstate"}
                    {:ssh_registration (assoc (:ssh_registration request) :ssh_resource_reference "wrong")}]]
      (is (thrown? Exception (node/node-plan opts (merge request change)))))))
(defn harness [directory]
  (let [[opts request] (inputs directory "digitalocean") plan (node/node-plan opts request)
        actions (atom ["create"]) current (atom nil) calls (atom [])
        state {:version 4 :serial 1 :lineage "fixture" :resources [{:type "digitalocean_droplet"}]
               :outputs {:compute_identity (get-in plan [:documents "compute.tf.json" :output :compute_identity])
                         :params {:value {:provider "digitalocean" :node_id "app-0" :name "node" :ip "192.0.2.1" :user "root" :sudoer "root"}}}}
        runner (fn [args cwd env _]
                 (swap! calls conj args)
                 (is (= "tofu" (first args)))
                 (is (nil? (get env "COLORS_PAR_SSH_PASSPHRASE")))
                 (is (nil? (get env "TF_VAR_keys_access_key")))
                 (when (= "apply" (second args))
                   (reset! current (if (= ["delete"] @actions) (assoc state :resources [] :outputs {}) state))
                   (spit (str cwd "/" (:state_filename request)) (json/generate-string @current)))
                 {:exit 0 :err "" :out (case (second args)
                                        "state" (if @current (json/generate-string @current) "")
                                        "show" (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions @actions}}]})
                                        "{}")})]
    {:opts opts :request request :plan plan :actions actions :calls calls :runner runner :env {"COLORS_PAR_DO_TOKEN" "fixture-secret" "COLORS_PAR_SSH_PASSPHRASE" "never-forward"}}))
(deftest compute-lifetime-excludes-ssh-ownership
  (let [dir (temp-dir) {:keys [opts request plan runner actions env]} (harness dir)]
    (try
      (let [result (node/compute-node! opts request "create" env {:runner runner})]
        (is (= "ready" (:status result)))
        (is (nil? (get-in result [:params :ssh_identity_file])))
        (is (not (.exists (java.io.File. (str (:directory plan) "/ssh-key")))))
        (is (= "ready" (:status (node/compute-node! opts request "inspect" env {:runner runner}))))
        (reset! actions ["delete"])
        (is (= "destroyed" (:status (node/compute-node! (assoc opts :compute-prevent-destroy false) request "delete" env {:runner runner}))))
        (is (.exists (java.io.File. (str (:directory plan) "/compute.tf.json")))))
      (finally (remove-tree dir)))))
(deftest guards-never-apply
  (doseq [mode [:replace :missing :protected :required]]
    (let [dir (temp-dir) {:keys [opts request runner actions calls env]} (harness dir)
          opts (cond-> opts (= mode :missing) (assoc :compute-prevent-destroy false) (= mode :required) (assoc :compute-require-existing-state true))]
      (try
        (when (= mode :replace) (reset! actions ["delete" "create"]))
        (is (= "error" (:status (node/compute-node! opts request (if (contains? #{:missing :protected} mode) "delete" "create") env {:runner runner}))))
        (is (not-any? #(= "apply" (second %)) @calls))
        (finally (remove-tree dir))))))
(deftest immutable-build-and-backend
  (let [dir (temp-dir) [opts request] (inputs dir "digitalocean")]
    (try
      (node/build-node! opts request)
      (is (thrown? Exception (node/build-node! (assoc opts :provider-backend "s3" :s3-bucket "other" :s3-region "eu-west-1") request)))
      (is (thrown? Exception (node/build-node! opts (-> request (assoc-in [:ssh_resource :reference] "other") (assoc-in [:ssh_registration :ssh_resource_reference] "other")))))
      (finally (remove-tree dir)))))
(deftest registration-independent-lifecycle
  (let [dir (temp-dir) [opts _] (inputs dir "digitalocean")
        request {:name "access" :workdir dir :state_filename "registration.tfstate" :ssh_resource identity}
        plan (node/registration-plan opts request) deleting (atom false) current (atom nil)
        state {:version 4 :serial 1 :lineage "registration-test" :resources [{:type "digitalocean_ssh_key"}]
               :outputs {:compute_identity (get-in plan [:documents "compute.tf.json" :output :compute_identity])
                         :params {:value {:provider "digitalocean" :node_id "registration-access" :id "12345"}}}}
        runner (fn [args cwd _ _]
                 (is (= "tofu" (first args)))
                 (when (= "apply" (second args))
                   (reset! current (if @deleting (assoc state :resources [] :outputs {}) state))
                   (spit (str cwd "/" (:state_filename request)) (json/generate-string @current)))
                 {:exit 0 :out (case (second args) "state" (if @current (json/generate-string @current) "")
                                "show" (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions [(if @deleting "delete" "create")]}}]}) "{}") :err ""})
        env {"COLORS_PAR_DO_TOKEN" "fixture-secret"}]
    (try
      (let [result (node/compute-registration! opts request "create" env {:runner runner})]
        (is (= "ready" (:status result))) (is (= "12345" (:id result)))
        (is (= (:reference identity) (:ssh_resource_reference result)))
        (is (= "ready" (:status (node/compute-registration! opts request "inspect" env {:runner runner}))))
        (reset! deleting true)
        (is (= "destroyed" (:status (node/compute-registration! (assoc opts :compute-prevent-destroy false) request "delete" env {:runner runner})))))
      (finally (remove-tree dir)))))

(deftest registration-public-default-arities
  (let [dir (temp-dir) [opts _] (inputs dir "digitalocean") request {:name "access" :workdir dir :state_filename "registration.tfstate" :ssh_resource identity}]
    (try
      (is (= "built" (:status (node/compute-registration! opts request "build"))))
      (is (= "built" (:status (node/compute-registration! opts request "build" {}))))
      (is (= "built" (:status (node/compute-registration! opts request "build" {} {}))))
      (finally (remove-tree dir)))))
(deftest synthetic-uninitialized-state-requires-confirmed-backend-absence
  (doseq [mode [:absent :present :read-failure :malformed :nonempty :unknown-field]
          operation ["create" "inspect" "delete"]]
    (let [dir (temp-dir) {:keys [opts request plan runner env]} (harness dir)
          opts (assoc opts :provider-backend "r2" :r2-bucket "fixture" :r2-endpoint "https://fixture.r2.cloudflarestorage.com" :compute-prevent-destroy false)
          env (assoc env "COLORS_PAR_R2_ACCESS_KEY_ID" "fixture-access" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "fixture-secret")
          stub {:version 4 :terraform_version "1.11.2" :serial 0 :lineage "" :outputs {} :resources [] :check_results nil}
          calls (atom []) applied (atom false)
          run (fn [args cwd child timeout]
                (swap! calls conj args)
                (cond
                  (= "aws" (first args)) (case mode
                                           :present {:exit 0 :out "{}" :err ""}
                                           :read-failure {:exit 1 :out "" :err "An error occurred (AccessDenied) when calling the GetObject operation: denied"}
                                           {:exit 1 :out "" :err "An error occurred (NoSuchKey) when calling the GetObject operation: missing"})
                  (and (= "state" (second args)) (not @applied))
                  {:exit 0 :out (if (= mode :malformed) "{malformed" (json/generate-string (cond-> stub (= mode :nonempty) (assoc :outputs {:unexpected {:value true}}) (= mode :unknown-field) (assoc :unexpected true)))) :err ""}
                  :else (do (when (= "apply" (second args)) (reset! applied true)) (runner args cwd child timeout))))]
      (try
        (let [result (node/compute-node! opts request operation env {:runner run})]
          (is (= (if (and (= mode :absent) (= operation "create")) "ready" "error") (:status result)) (str mode " " operation))
          (is (= (and (= mode :absent) (= operation "create")) @applied))
          (when (contains? #{:malformed :nonempty :unknown-field} mode)
            (is (not-any? #(= "aws" (first %)) @calls))))
        (finally (remove-tree dir))))))
