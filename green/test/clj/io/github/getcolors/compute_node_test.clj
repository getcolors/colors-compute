(ns io.github.getcolors.compute-node-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [clojure.string :as str]
            [io.github.getcolors.compute-node :as node]
            [io.github.getcolors.compute-diagnostics :as diagnostics]
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

(deftest live-connection-does-not-apply-or-use-stale-outputs
  (doseq [[provider kind attribute] [["aws" "aws_instance" :id] ["azure" "azurerm_linux_virtual_machine" :virtual_machine_id]
                                    ["digitalocean" "digitalocean_droplet" :id] ["google" "google_compute_instance" :instance_id]
                                    ["hcloud" "hcloud_server" :id] ["oci" "oci_core_instance" :id]
                                    ["vultr" "vultr_instance" :id] ["yandex" "yandex_compute_instance" :id]]
          mode [:ready :changed :gone :unknown :empty-ip :wrong-identity :failed-plan :failed-show :detached :errored :missing-snapshot]]
    (let [dir (temp-dir) [opts request] (inputs dir provider)
          plan (node/node-plan opts request)
          ownership (get-in plan [:documents "compute.tf.json" :output :compute_identity])
          params {:provider provider :node_id "app-0" :name "node" :ip "192.0.2.1" :user "ubuntu" :sudoer "ubuntu" :provider_id "owned"}
          resource {:mode "managed" :type kind :name "node"}
          state {:version 4 :serial 1 :lineage "connection-test"
                 :resources [(assoc resource :instances [{:attributes {attribute "immutable"}}])]
                 :outputs {:compute_identity ownership :params {:value params}}}
          refreshed {:format_version "1.2"
                     :planned_values {:root_module {:resources (if (= mode :gone) [] [(assoc resource :values (assoc {:id "vm-path" :network_interface_ids ["nic-path"]
                                      :network_interface [{:access_config [{:nat_ip (if (= mode :detached) "192.0.2.3" "192.0.2.2")}]}]} attribute (if (= mode :changed) "replacement" "immutable")))])}
                                      :outputs {:compute_identity (if (= mode :wrong-identity) {:value {}} ownership)
                                                :params {:value (assoc params :ip (if (= mode :empty-ip) "" "192.0.2.2"))}}}
                     :output_changes {:params {:after_unknown (if (= mode :unknown) {:ip true} false)}}}
          refreshed (if (= provider "azure")
                      (update-in refreshed [:planned_values :root_module :resources] into
                                 [{:mode "managed" :type "azurerm_network_interface" :name "node"
                                   :values {:id "nic-path" :virtual_machine_id (if (= mode :detached) "other-vm" "vm-path")
                                            :ip_configuration [{:public_ip_address_id "ip-path"}]}}
                                  {:mode "managed" :type "azurerm_public_ip" :name "node" :values {:id "ip-path" :ip_address "192.0.2.2"}}]) refreshed)
          ;; Match real refresh-only show JSON: outputs are planned, resources
          ;; are the refreshed prior_state, and planned root_module is null.
          refreshed (-> refreshed
                        (assoc :errored (= mode :errored)
                               :prior_state {:format_version "1.0"
                                             :values {:root_module (get-in refreshed [:planned_values :root_module])}})
                        (assoc-in [:planned_values :root_module] nil))
          refreshed (if (= mode :missing-snapshot)
                      (-> refreshed
                          (assoc-in [:planned_values :root_module] (get-in refreshed [:prior_state :values :root_module]))
                          (dissoc :prior_state)) refreshed)
          env (into {} (map (fn [[key _]] [(str "COLORS_PAR_" (str/upper-case (str/replace (name key) "-" "_"))) "fixture-secret"])
                           (get-in io.github.getcolors.compute/registry [:compute (keyword provider) :tofu-env])))
          calls (atom [])
          runner (fn [args _ _ _]
                   (swap! calls conj args)
                   (if (or (and (= mode :failed-plan) (= "plan" (second args))) (and (= mode :failed-show) (= "show" (second args))))
                     {:exit 1 :out "" :err "failure"}
                     {:exit 0 :out (case (second args) "state" (json/generate-string state) "show" (json/generate-string refreshed) "{}") :err ""}))]
      (try
        (let [result (node/resolve-connection! opts request env {:runner runner})]
          (is (= (if (or (= mode :ready) (and (= mode :detached) (not (contains? #{"google" "azure"} provider)))) "ready" "error") (:status result)) (str provider " " mode " " result))
          (when (= mode :ready) (is (= "192.0.2.2" (get-in result [:params :ip]))))
          (is (not-any? #(= "apply" (second %)) @calls))
          (is (some #(and (= "plan" (second %)) (some #{"-refresh-only"} %) (some #{"-lock-timeout=60s"} %)) @calls))
          (is (not (.exists (java.io.File. (str (:directory plan) "/connection.tfplan")))))
          (is (not (.exists (java.io.File. (str (:directory plan) "/credentials.tfbackend.json"))))))
        (finally (remove-tree dir))))))

(deftest google-reauth-diagnostics
  (doseq [{:keys [name argv provider result expected]} (json/parse-string (slurp "../test/fixtures/reauth.json") true)]
    (testing name
      (binding [diagnostics/*context* (atom {:opts {:provider-compute provider} :source {}})]
        ((diagnostics/wrap-runner (fn [& _] result)) argv "/tmp" {} 100)
        (let [details (:last-command @diagnostics/*context*)]
          (is (= expected (:auth_reason details)))
          (is (not (str/includes? (pr-str details) "PRIVATE-CANARY")))
          (is (not (str/includes? (pr-str details) "PRIVATE-STDOUT"))))))))

(deftest connection-resolution-preserves-reauth-reason
  (let [dir (temp-dir) [opts request] (inputs dir "google") plan (node/node-plan opts request)
        state {:version 4 :serial 1 :lineage "reauth-fixture" :resources [{:type "google_compute_instance"}]
               :outputs {:compute_identity (get-in plan [:documents "compute.tf.json" :output :compute_identity])
                         :params {:value {:provider "google"}}}}
        calls (atom [])]
    (try
      (node/build-node! opts request)
      (spit (str (:directory plan) "/" (:state_filename request)) (json/generate-string state))
      (let [result (node/resolve-connection! opts request {}
                     {:runner (fn [args _ _ _]
                                (swap! calls conj (second args))
                                (case (second args)
                                  "plan" {:exit 1 :out "PRIVATE-STDOUT" :err "oauth2: invalid_grant invalid_rapt\n{\"private_key\":\"PRIVATE-CANARY\"}"}
                                  "state" {:exit 0 :out (json/generate-string state) :err ""}
                                  {:exit 0 :out "" :err ""}))})]
        (is (= "google_reauth_required" (get-in result [:error :auth_reason])))
        (is (= "none" (get-in result [:error :infrastructure_changes])))
        (is (= ["init" "state" "plan"] @calls))
        (is (not (str/includes? (pr-str result) "PRIVATE-CANARY"))))
      (finally (remove-tree dir)))))

(deftest inconsistent-state-is-actionable-without-mutation
  (doseq [registration? [false true] operation ["create" "inspect" "delete"]]
    (let [dir (temp-dir) [base request] (inputs dir "digitalocean")
          opts (assoc base :compute-prevent-destroy false)
          request (if registration? {:name "access" :workdir dir :state_filename "registration.tfstate" :ssh_resource identity} request)
          planner (if registration? node/build-registration! node/build-node!)
          lifecycle (if registration? node/compute-registration! node/compute-node!)
          plan (planner opts request)
          text (json/generate-string {:version 4 :serial 1 :lineage "fixture" :resources [] :outputs {:compute_identity {:value "SECRET_STATE_CONTENT"}}})
          path (str (:directory plan) "/" (:state_filename request)) calls (atom [])]
      (try
        (spit path text)
        (let [result (lifecycle opts request operation {"COLORS_PAR_DO_TOKEN" "fixture"}
                                {:runner (fn [args & _] (swap! calls conj args) {:exit 0 :err "" :out (if (= "state" (second args)) text "{}")})})]
          (is (= "state_inconsistent" (get-in result [:error :code])))
          (is (= "state" (get-in result [:error :stage])))
          (is (= "none" (get-in result [:error :infrastructure_changes])))
          (is (str/includes? (get-in result [:error :message]) "Back up"))
          (is (not (str/includes? (json/generate-string result) "SECRET_STATE_CONTENT")))
          (is (not-any? #(contains? #{"plan" "apply" "destroy"} (second %)) @calls))
          (is (= text (slurp path))))
        (finally (remove-tree dir))))))

(deftest inconsistent-state-after-apply-retains-possible-changes
  (let [dir (temp-dir) {:keys [opts request runner env]} (harness dir) applied? (atom false)]
    (try
      (let [result (node/compute-node! opts request "create" env
                    {:runner (fn [args cwd env timeout]
                               (when (= "apply" (second args)) (reset! applied? true))
                               (let [r (runner args cwd env timeout)]
                                 (if (and @applied? (= "state" (second args)))
                                   (update r :out #(json/generate-string (assoc (json/parse-string % true) :resources []))) r)))})]
        (is (= "state_inconsistent" (get-in result [:error :code])))
        (is (= "possible" (get-in result [:error :infrastructure_changes]))))
      (finally (remove-tree dir)))))

(defn retry-state [plan]
  (let [owner (get-in plan [:documents "compute.tf.json" :output :compute_identity :value])]
    {:version 4 :serial 1 :lineage "failed-first-apply" :resources []
     :outputs {:compute_identity {:value owner :sensitive false
                                  :type ["object" (into {} (map (fn [k] [k "string"]) (keys owner)))]}}}))

(deftest failed-first-apply-retries-without-resetting-state
  (doseq [registration? [false true] metadata? [false true]]
    (let [dir (temp-dir) [opts original] (inputs dir "digitalocean")
          request (if registration? {:name "access" :workdir dir :state_filename "registration.tfstate" :ssh_resource identity} original)
          planner (if registration? node/build-registration! node/build-node!)
          lifecycle (if registration? node/compute-registration! node/compute-node!)
          plan (planner opts request) pending (cond-> (retry-state plan) (not metadata?) (update-in [:outputs :compute_identity] dissoc :sensitive :type))
          path (str (:directory plan) "/" (:state_filename request))
          calls (atom []) applies (atom 0) current (atom nil)
          complete (-> pending (assoc :serial 2 :resources [{:type (if registration? "digitalocean_ssh_key" "digitalocean_droplet")}])
                       (assoc-in [:outputs :params] {:value (if registration?
                         {:provider "digitalocean" :node_id "registration-access" :id "12345"}
                         {:provider "digitalocean" :node_id "app-0" :name "node" :ip "192.0.2.1" :user "root" :sudoer "root"})}))
          runner (fn [args & _]
                   (swap! calls conj args)
                   (case (second args)
                     "state" {:exit 0 :err "" :out (if @current (slurp path) "")}
                     "show" {:exit 0 :err "" :out (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions ["create"]}}]})}
                     "apply" (let [first? (= 1 (swap! applies inc))]
                               (when-not first? (is (= pending (json/parse-string (slurp path) true))))
                               (reset! current (if first? pending complete))
                               (spit path (json/generate-string @current))
                               {:exit (if first? 1 0) :err (if first? "provider authentication failed" "") :out ""})
                     {:exit 0 :err "" :out "{}"}))]
      (try
        (let [first-result (lifecycle opts request "create" {"COLORS_PAR_DO_TOKEN" "fixture"} {:runner runner})]
          (is (= "error" (:status first-result)))
          (is (= "possible" (get-in first-result [:error :infrastructure_changes])))
          (is (= pending (json/parse-string (slurp path) true))))
        (reset! calls [])
        (is (= "ready" (:status (lifecycle opts request "create" {"COLORS_PAR_DO_TOKEN" "fixture"} {:runner runner}))))
        (is (= ["init" "state" "plan" "show" "apply" "state"] (mapv second @calls)))
        (is (= complete (json/parse-string (slurp path) true)))
        (is (= 2 @applies))
        (finally (remove-tree dir))))))

(deftest retry-state-rejects-other-identities-outputs-and-operations
  (doseq [registration? [false true]
          mode [:wrong-identity :missing-field :extra-field :extra-output :null-output :sensitive :wrong-type :extra-wrapper-field :malformed :required :inspect :delete :resolve-connection]]
    (let [dir (temp-dir) [base original] (inputs dir "digitalocean") opts (cond-> (assoc base :compute-prevent-destroy false) (= mode :required) (assoc :compute-require-existing-state true))
          request (if registration? {:name "access" :workdir dir :state_filename "registration.tfstate" :ssh_resource identity} original)
          planner (if registration? node/build-registration! node/build-node!)
          lifecycle (if registration? node/compute-registration! node/compute-node!)
          plan (planner opts request) pending (retry-state plan)
          state (case mode
                  :wrong-identity (assoc-in pending [:outputs :compute_identity :value :profile] "other")
                  :missing-field (update-in pending [:outputs :compute_identity :value] dissoc :provider)
                  :extra-field (assoc-in pending [:outputs :compute_identity :value :unexpected] "other")
                  :extra-output (assoc-in pending [:outputs :unexpected] {:value true})
                  :null-output (assoc-in pending [:outputs :compute_identity] nil)
                  :sensitive (assoc-in pending [:outputs :compute_identity :sensitive] true)
                  :wrong-type (assoc-in pending [:outputs :compute_identity :type] "string")
                  :extra-wrapper-field (assoc-in pending [:outputs :compute_identity :unexpected] true)
                  :malformed (assoc pending :lineage "")
                  pending)
          text (json/generate-string state) path (str (:directory plan) "/" (:state_filename request)) calls (atom [])
          operation (if (contains? #{:inspect :delete :resolve-connection} mode) (name mode) "create")]
      (try
        (spit path text)
        (let [result (lifecycle opts request operation {"COLORS_PAR_DO_TOKEN" "fixture"}
                      {:runner (fn [args & _] (swap! calls conj args) {:exit 0 :err "" :out (if (= "state" (second args)) text "{}")})})]
          (is (= "error" (:status result)) (str registration? " " mode))
          (when (= mode :required) (is (= "state_absent" (get-in result [:error :code]))))
          (when-not (or (contains? #{:malformed :required} mode) (and registration? (= mode :resolve-connection)))
            (is (= "state_inconsistent" (get-in result [:error :code])) (str registration? " " mode)))
          (is (= "none" (get-in result [:error :infrastructure_changes])))
          (is (not-any? #(contains? #{"plan" "apply" "destroy"} (second %)) @calls))
          (is (= text (slurp path))))
        (finally (remove-tree dir))))))

(deftest successful-apply-must-not-leave-only-retry-identity
  (let [dir (temp-dir) {:keys [opts request plan runner env]} (harness dir) applied? (atom false)]
    (try
      (let [result (node/compute-node! opts request "create" env
                    {:runner (fn [args cwd child timeout]
                               (when (= "apply" (second args)) (reset! applied? true))
                               (if (and @applied? (= "state" (second args)))
                                 {:exit 0 :err "" :out (json/generate-string (retry-state plan))}
                                 (runner args cwd child timeout)))})]
        (is (= "state_inconsistent" (get-in result [:error :code])))
        (is (= "possible" (get-in result [:error :infrastructure_changes]))))
      (finally (remove-tree dir)))))

(deftest output-secrets-distinguish-configuration-from-credentials
  (doseq [{:keys [name environment allowed]}
          (json/parse-string (slurp "../test/fixtures/node-output-environment.json") true)]
    (testing name
      (let [dir (temp-dir) {:keys [opts request runner env calls]} (harness dir)]
        (try
          (is (= "ready" (:status (node/compute-node! opts request "create" env {:runner runner}))))
          (reset! calls [])
          (let [source (merge env (into {} (map (fn [[k v]] [(clojure.core/name k) v]) environment)))
                result (node/compute-node! opts request "inspect" source {:runner runner})]
            (is (= allowed (= "ready" (:status result))))
            (is (not-any? #(= "apply" (second %)) @calls)))
          (finally (remove-tree dir)))))))
