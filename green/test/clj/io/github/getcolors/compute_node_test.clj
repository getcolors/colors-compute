(ns io.github.getcolors.compute-node-test
  (:require [clojure.test :refer [deftest is testing]]
            [cheshire.core :as json]
            [clojure.string :as str]
            [io.github.getcolors.compute-diagnostics :as diagnostics]
            [io.github.getcolors.compute-node :as node]
            [io.github.getcolors.compute-local :as local])
  (:import [java.nio.file Files Path]))

(def fixtures (json/parse-string (slurp "../test/fixtures/provider-requests.json") true))
(defn inputs []
  (let [[opts _ request] (:args (first fixtures))]
    [(assoc opts :provider-backend "s3" :s3-bucket "state-fixture" :s3-region "eu-west-1" :s3-prefix "infrastructure")
     (assoc (select-keys request [:node_id :security :network]) :workdir "/tmp/colors-sdk" :state_filename "app-0.tfstate")]))
(defn temp-dir [] (str (.toRealPath (Files/createTempDirectory "compute-node-test-" (local/attrs "rwx------")) (make-array java.nio.file.LinkOption 0))))
(defn remove-tree [root]
  (with-open [paths (Files/walk (local/path root) (make-array java.nio.file.FileVisitOption 0))]
    (doseq [p (reverse (sort-by #(.getNameCount ^Path %) (iterator-seq (.iterator paths))))] (Files/deleteIfExists p))))
(defn document [opts request]
  {:version 4 :serial 1 :lineage "node-fixture"
   :outputs {:ssh_public_key_fingerprint {:value "SHA256:I5Fc5K9BPO3/6VVAknkArklFt4W1mr/asX4X5QK1Qdg"}
             :compute_identity {:value {:profile (:profile opts) :node_id (:node_id request) :state_filename (:state_filename request) :provider (:provider-compute opts)}}
             :params {:value {:provider (:provider-compute opts) :node_id (:node_id request) :ip "192.0.2.1" :name "app" :user "ubuntu" :sudoer "ubuntu"}}}
   :resources (into [{:mode "managed" :type "tls_private_key" :name "machine"
                     :instances [{:attributes {:public_key_openssh "ssh-ed25519 AAAATEST"}}]}]
                    (for [[kind suffix] [["private" "ssh-key"] ["public" "ssh-key.pub"]]]
                      {:mode "managed" :type "aws_s3_object" :name (str "ssh_" kind)
                       :instances [{:attributes {:bucket (:s3-bucket opts)
                                                 :key (str (:s3-prefix opts) "/" (:profile opts) "/" (:node_id request) "/" suffix)}}]}))})
(defn error-result? [result] (and (= "error" (:status result)) (map? (:error result)) (string? (get-in result [:error :code]))))
(def no-key {:exit 254 :out "" :err "An error occurred (NoSuchKey) when calling the GetObject operation: absent"})

(deftest independent-provider-roots
  (doseq [{:keys [name args]} fixtures :when (= "shared" (second args))]
    (testing name
      (let [[opts _ request] args
            plan (node/node-plan (assoc opts :provider-backend "s3" :s3-bucket "fixture" :s3-region "eu-west-1")
                                 (assoc (select-keys request [:node_id :security :network]) :workdir "/tmp/sdk" :state_filename "node.tfstate"))
            root (get-in plan [:documents "compute.tf.json"])]
        (is (= "ED25519" (get-in root [:resource :tls_private_key :machine :algorithm])))
        (is (= false (get-in root [:resource :aws_s3_object :ssh_private :force_destroy])))
        (is (not (str/includes? (json/generate-string root) "sentinel")))
        (is (= "${tls_private_key.machine.private_key_openssh}" (get-in root [:resource :aws_s3_object :ssh_private :content])))
        (doseq [[kind instances] (:resource root) :when (not (contains? #{:tls_private_key :aws_s3_object} kind)) [_ config] instances]
          (is (some #{"aws_s3_object.ssh_private"} (:depends_on config))))))))

(deftest isolation-and-immutable-build-identity
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)]
    (try
      (let [plan (node/build-node! opts request) root (:directory plan)]
        (is (= (str "infrastructure/" (:profile opts) "/app-0.tfstate") (:state_key plan)))
        (is (= (str directory "/" (:profile opts) "/" (:node_id request)) root))
        (is (.isFile (java.io.File. (str root "/compute.tf.json"))))
        (spit (str root "/ssh-key") "stale key")
        (node/build-node! opts request)
        (is (= "stale key" (slurp (str root "/ssh-key"))))
        (is (thrown? Exception (node/build-node! (assoc opts :s3-prefix "other") request)))
        (is (thrown? Exception (node/build-node! opts (assoc request :state_filename "other.tfstate")))))
      (doseq [bad [(assoc request :node_id "../escape") (assoc request :state_filename "../escape.tfstate")
                   (assoc request :roles {}) (assoc request :workdir "relative")]]
        (is (thrown? Exception (node/node-plan opts bad))))
      (finally (remove-tree directory)))))

(deftest persistent-run-and-authoritative-access
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)
        state (document opts request) calls (atom [])
        runner (fn [argv cwd env _]
                 (swap! calls conj {:argv argv :cwd cwd :env env})
                 (cond
                   (= ["tofu" "state" "pull"] argv) {:exit 0 :out (json/generate-string state)}
                   (= ["ssh-keygen" "-y"] (subvec argv 0 2)) {:exit 0 :out "ssh-ed25519 AAAATEST"}
                   (= "aws" (first argv)) (do (spit (last argv) (if (str/ends-with? (last argv) "pub.download") "ssh-ed25519 AAAATEST" "REMOTE PRIVATE")) {:exit 0 :out "{}"})
                   :else {:exit 0 :out ""}))]
    (try
      (let [plan (node/build-node! opts request)]
        (spit (str (:directory plan) "/ssh-key") "STALE")
        (let [result (node/compute-node! opts request "prepare-access" {"PATH" "/bin" "TF_DATA_DIR" "/evil"} {:runner runner})]
          (is (= "REMOTE PRIVATE" (slurp (str (:directory result) "/ssh-key"))))
          (is (not (str/includes? (pr-str result) "REMOTE PRIVATE"))))
        (is (every? #(= (:directory plan) (:cwd %)) @calls))
        (is (every? #(not (contains? (:env %) "TF_DATA_DIR")) @calls))
        (is (.isFile (java.io.File. (str (:directory plan) "/compute.tf.json"))))
        (let [fail-runner (fn [argv cwd env timeout]
                            (if (and (= "aws" (first argv)) (str/ends-with? (last argv) "pub.download"))
                              {:exit 1 :out "" :err "denied"} (runner argv cwd env timeout)))]
          (is (error-result? (node/compute-node! opts request "prepare-access" {} {:runner fail-runner})))
          (is (not (.exists (java.io.File. (str (:directory plan) "/ssh-key")))))))
      (finally (remove-tree directory)))))

(deftest mutation-refusals
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)
        calls (atom []) state (document opts request)
        runner (fn [argv _ _ _]
                 (swap! calls conj argv)
                 (cond (= ["tofu" "state" "pull"] argv) {:exit 0 :out (json/generate-string state)}
                       (= ["tofu" "show"] (subvec argv 0 2)) {:exit 0 :out (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions ["delete" "create"]}}]})}
                       :else {:exit 0 :out ""}))]
    (try
      (is (error-result? (node/compute-node! opts request "delete" {} {:runner runner})))
      (is (empty? @calls))
      (is (error-result? (node/compute-node! opts request "create" {} {:runner runner})))
      (is (not-any? #(= ["tofu" "apply"] (subvec % 0 2)) @calls))
      (reset! calls [])
      (let [mismatch (assoc-in state [:outputs :compute_identity :value :provider] "oci")]
        (is (error-result? (node/compute-node! opts request "create" {}
                               {:runner (fn [argv & _] (swap! calls conj argv)
                                          {:exit 0 :out (if (= ["tofu" "state" "pull"] argv) (json/generate-string mismatch) "")})})))
        (is (not-any? #(= ["tofu" "plan"] (subvec % 0 2)) @calls)))
      (finally (remove-tree directory)))))

(deftest orphan-remote-keys-are-not-overwritten
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory) calls (atom [])]
    (try
      (is (error-result?
            (node/compute-node! opts request "create" {}
              {:runner (fn [argv & _]
                         (swap! calls conj argv)
                         (cond (= ["tofu" "state" "pull"] argv) {:exit 1 :out ""}
                               (= "aws" (first argv)) (if (some #(= "infrastructure/example/app-0.tfstate" %) argv) no-key {:exit 0 :out "{}"})
                               :else {:exit 0 :out ""}))})))
      (is (not-any? #(= ["tofu" "plan"] (subvec % 0 2)) @calls))
      (finally (remove-tree directory)))))

(deftest create-and-delete-retain-templates
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)
        state (atom (document opts request)) operation (atom "create") calls (atom [])
        runner (fn [argv & _]
                 (swap! calls conj argv)
                 (cond
                   (= ["tofu" "state" "pull"] argv) {:exit 0 :out (json/generate-string @state)}
                   (= ["tofu" "show"] (subvec argv 0 2)) {:exit 0 :out (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions [(if (= @operation "delete") "delete" "update")]}}]})}
                   (= ["tofu" "apply"] (subvec argv 0 2)) (do (when (= @operation "delete") (swap! state assoc :resources [] :outputs {})) {:exit 0 :out ""})
                   (= "aws" (first argv)) (do (spit (last argv) (if (str/ends-with? (last argv) "pub.download") "ssh-ed25519 AAAATEST" "REMOTE PRIVATE")) {:exit 0 :out "{}"})
                   (= "ssh-keygen" (first argv)) {:exit 0 :out "ssh-ed25519 AAAATEST"}
                   :else {:exit 0 :out ""}))]
    (try
      (let [result (node/compute-node! opts request "create" {} {:runner runner})]
        (is (= "ready" (:status result)))
        (is (= "REMOTE PRIVATE" (slurp (get-in result [:params :ssh_identity_file]))))
        (is (some #(= ["tofu" "apply"] (subvec % 0 2)) @calls))
        (reset! operation "delete")
        (is (= "destroyed" (:status (node/compute-node! (assoc opts :compute-prevent-destroy false) request "delete" {} {:runner runner}))))
        (is (.isFile (java.io.File. (str (:directory result) "/compute.tf.json"))))
        (is (.isFile (java.io.File. (str (:directory result) "/backend.tf.json"))))
        (is (not (.exists (java.io.File. (str (:directory result) "/ssh-key"))))))
      (finally (remove-tree directory)))))

(deftest cancellation-and-sensitive-output-refusal
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)]
    (try
      (is (thrown? InterruptedException (node/compute-node! opts request "create" {}
                                         {:runner (fn [& _] (throw (InterruptedException. "cancelled")))})))
      (let [state (assoc-in (document opts request) [:outputs :params :value :name] "hidden-token")]
        (is (error-result?
               (node/compute-node! opts request "inspect" {"COLORS_PAR_DO_TOKEN" "hidden-token"}
                 {:runner (fn [argv & _] {:exit 0 :out (if (= ["tofu" "state" "pull"] argv) (json/generate-string state) "")})}))))
      (finally (remove-tree directory)))))

(deftest exact-remote-absence-and-provider-output-contract
  (let [directory (temp-dir) presence (deref (ns-resolve 'io.github.getcolors.compute-node 'object-presence!))
        storage {:kind "s3" :bucket "fixture" :region "eu-west-1"}]
    (try
      (doseq [message ["An error occurred (NoSuchKey) when calling the GetObject operation: absent"
                       "  aws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): absent"]]
        (is (= "absent" (presence storage "node.tfstate" directory {} (fn [& _] {:exit 1 :out "" :err message})))))
      (doseq [message ["An error occurred (NoSuchKey) when calling the GetObject operationX: absent"
                       "An error occurred (NoSuchKey) when calling the GetObject operation"
                       "prefix An error occurred (NoSuchKey) when calling the GetObject operation: absent"]]
        (is (thrown? Exception (presence storage "node.tfstate" directory {} (fn [& _] {:exit 1 :out "" :err message})))))
      (let [[opts request] (inputs) request (assoc request :workdir directory)
            state (assoc-in (document opts request) [:outputs :params :value :instance_id] "i-owned")
            result (node/compute-node! opts request "inspect" {}
                     {:runner (fn [argv & _] {:exit 0 :out (if (= ["tofu" "state" "pull"] argv) (json/generate-string state) "")})})]
        (is (= "i-owned" (get-in result [:params :instance_id]))))
      (finally (remove-tree directory)))))

(deftest authoritative-key-storage-cannot-be-redirected
  (let [[opts request] (inputs)]
    (doseq [backend ["s3" "r2" "oci" "local"] field [:ssh-s3-bucket :ssh-s3-region :ssh-s3-endpoint]]
      (is (thrown? Exception (node/node-plan (assoc opts :provider-backend backend field "override") request))))
    (doseq [backend ["gcs"]
            :let [explicit (assoc opts :provider-backend backend :gcs-bucket "state-bucket" :gcs-region "eu-west-1"
                                  :google-project "test-project" :ssh-s3-bucket "key-bucket" :ssh-s3-region "eu-west-1")]]
      (doseq [endpoint ["https://objects.example" "https://objects.example/"]]
        (is (= "planned" (:status (node/node-plan (assoc explicit :ssh-s3-endpoint endpoint) request)))))
      (doseq [endpoint ["http://objects.example" "https://user:password@objects.example" "https://objects.example/path"
                        "https://objects.example?query=value" "https://objects.example#fragment"]]
        (is (thrown? Exception (node/node-plan (assoc explicit :ssh-s3-endpoint endpoint) request))))
      (doseq [field [:ssh-s3-bucket :ssh-s3-region] value [nil "" " "]]
        (is (thrown? Exception (node/node-plan (assoc explicit field value) request)))))))

(deftest local-keys-are-owned-by-local-state
  (let [[opts request] (inputs) opts (assoc opts :provider-backend "local") directory (temp-dir)
        request (assoc request :workdir directory) plan (node/node-plan opts request)
        root (get-in plan [:documents "compute.tf.json"])
        state (-> (document opts request)
                  (update :resources #(vec (filter (fn [resource] (= "tls_private_key" (:type resource))) %)))
                  (assoc-in [:outputs :ssh_private_key] {:value "STATE PRIVATE" :sensitive true})
                  (assoc-in [:outputs :ssh_public_key] {:value "ssh-ed25519 AAAATEST" :sensitive true}))
        calls (atom []) runner (fn [argv & _]
                                 (swap! calls conj argv)
                                 {:exit 0 :out (cond (= ["tofu" "state" "pull"] argv) (json/generate-string state)
                                                     (= "ssh-keygen" (first argv)) "ssh-ed25519 AAAATEST"
                                                     :else "")})]
    (try
      (is (= {} (:key_objects plan)))
      (is (nil? (:variable root)))
      (is (nil? (get-in root [:resource :aws_s3_object])))
      (is (= {:region (:aws-region opts)} (get-in root [:provider :aws])))
      (is (= true (get-in root [:output :ssh_private_key :sensitive])))
      (is (nil? (get-in root [:output :ssh_public_key :sensitive])))
      (is (= ["tls_private_key.machine"] (get-in root [:resource :aws_instance :node :depends_on])))
      (node/build-node! opts request)
      (spit (str (:directory plan) "/ssh-key") "STALE")
      (let [result (node/compute-node! opts request "prepare-access" {} {:runner runner})]
        (is (= "ready" (:status result)))
        (is (= "STATE PRIVATE" (slurp (get-in result [:params :ssh_identity_file]))))
        (is (not (str/includes? (json/generate-string result) "STATE PRIVATE"))))
      (is (not-any? #(= "aws" (first %)) @calls))
      (reset! calls [])
      (is (error-result?
             (node/compute-node! opts request "create" {}
                {:runner (fn [argv & _] (swap! calls conj argv)
                           (if (= ["tofu" "state" "pull"] argv) {:exit 1 :out ""} {:exit 0 :out ""}))})))
      (is (not-any? #(= ["tofu" "plan"] (subvec % 0 2)) @calls))
      (is (= "STATE PRIVATE" (slurp (str (:directory plan) "/ssh-key"))))
      (finally (remove-tree directory)))))

(deftest local-empty-state-recreation-and-malformed-state-refusal
  (let [[opts request] (inputs) opts (assoc opts :provider-backend "local") directory (temp-dir)
        request (assoc request :workdir directory) plan (node/build-node! opts request)
        complete (-> (document opts request)
                     (update :resources #(vec (filter (fn [resource] (= "tls_private_key" (:type resource))) %)))
                     (assoc-in [:outputs :ssh_private_key] {:value "NEW PRIVATE" :sensitive true})
                     (assoc-in [:outputs :ssh_public_key] {:value "ssh-ed25519 AAAATEST"}))
        state (atom (assoc complete :resources [] :outputs {})) calls (atom [])
        runner (fn [argv & _]
                 (swap! calls conj argv)
                 (cond (= ["tofu" "state" "pull"] argv) {:exit 0 :out (json/generate-string @state)}
                       (= ["tofu" "show"] (subvec argv 0 2)) {:exit 0 :out (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions ["create"]}}]})}
                       (= ["tofu" "apply"] (subvec argv 0 2)) (do (reset! state complete) {:exit 0 :out ""})
                       (= "ssh-keygen" (first argv)) {:exit 0 :out "ssh-ed25519 AAAATEST"}
                       :else {:exit 0 :out ""}))]
    (try
      (spit (str (:directory plan) "/ssh-key") "OLD COPY")
      (is (= "ready" (:status (node/compute-node! opts request "create" {} {:runner runner}))))
      (is (= "NEW PRIVATE" (slurp (str (:directory plan) "/ssh-key"))))
      (reset! state (assoc complete :resources []))
      (reset! calls [])
      (is (error-result? (node/compute-node! opts request "create" {} {:runner runner})))
      (is (not-any? #(= ["tofu" "plan"] (subvec % 0 2)) @calls))
      (reset! state (assoc-in complete [:outputs :ssh_private_key :sensitive] false))
      (is (error-result? (node/compute-node! opts request "prepare-access" {} {:runner runner})))
      (is (not (.exists (java.io.File. (str (:directory plan) "/ssh-key")))))
      (finally (remove-tree directory)))))

(deftest inspect-confirmed-empty-state-is-read-only
  (let [[opts request] (inputs) opts (assoc opts :provider-backend "local") directory (temp-dir)
        request (assoc request :workdir directory) plan (node/build-node! opts request)
        empty-state (assoc (document opts request) :resources [] :outputs {}) calls (atom [])
        runner (fn [argv & _]
                 (swap! calls conj argv)
                 {:exit 0 :out (if (= ["tofu" "state" "pull"] argv) (json/generate-string empty-state) "")})]
    (try
      (spit (str (:directory plan) "/ssh-key") "copy retained for explicit cleanup")
      (is (= {:status "destroyed" :directory (:directory plan)} (node/compute-node! opts request "inspect" {} {:runner runner})))
      (is (= [["tofu" "init"] ["tofu" "state"]] (mapv #(subvec % 0 2) @calls)))
      (is (= "copy retained for explicit cleanup" (slurp (str (:directory plan) "/ssh-key"))))
      (is (error-result? (node/compute-node! opts request "prepare-access" {} {:runner runner})))
      (is (error-result? (node/compute-node! opts request "inspect" {}
                                {:runner (fn [argv & _] {:exit (if (= ["tofu" "state" "pull"] argv) 1 0) :out ""})})))
      (finally (remove-tree directory)))))

(deftest native-shim-diagnostic-identifies-preapply-failure
  (let [[opts request] (inputs) opts (assoc opts :provider-backend "local") directory (temp-dir)
        request (assoc request :workdir directory) binary (str directory "/tofu")]
    (try
      (spit binary "#!/bin/sh\nprintf '%s\\n' 'No version is set for command tofu' 'Please consider adding a version to .tool-versions' >&2\nexit 126\n")
      (.setExecutable (java.io.File. binary) true true)
      (let [result (node/compute-node! opts request "create" {"PATH" directory}) error (:error result)]
        (is (= "error" (:status result)))
        (is (= "command_failed" (:code error)))
        (is (= "init" (:stage error)))
        (is (= "Required command failed." (:message error)))
        (is (= "none" (:infrastructure_changes error)))
        (is (= ["tofu" "init"] (:command error)))
        (is (= binary (:executable error)))
        (is (= 126 (:exit_code error)))
        (is (str/includes? (:stderr error) "No version is set for command tofu"))
        (is (not (contains? error :stdout))))
      (finally (remove-tree directory)))))

(deftest diagnostics-track-apply-risk-and-redact-command-data
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)
        state (document opts request)
        failure-stage (atom "apply")
        runner (fn [argv & _]
                 (cond
                   (= ["tofu" "state" "pull"] argv) {:exit 0 :out (json/generate-string state)}
                   (= ["tofu" "show"] (subvec argv 0 2)) {:exit 0 :out (if (= "plan-validation" @failure-stage) "{malformed-plan" (json/generate-string {:format_version "1.2" :planned_values {} :resource_changes [{:change {:actions ["update"]}}]}))}
                   (or (and (= "apply" @failure-stage) (= ["tofu" "apply"] (subvec argv 0 2)))
                       (and (= "access" @failure-stage) (= "aws" (first argv))))
                   {:exit 7 :out "NEVER INCLUDE THIS STATE OR PRIVATE KEY"
                    :err "provider refused: known-secret-token\npassword=unknown-password\n-----BEGIN OPENSSH PRIVATE KEY-----\nNEVER PEM BODY\n-----END OPENSSH PRIVATE KEY-----\n"}
                   :else {:exit 0 :out ""}))]
    (try
      (doseq [[stage code risk] [["apply" "command_failed" "possible"] ["access" "key_access_failed" "possible"] ["plan-validation" "unsafe_plan" "none"]]]
        (reset! failure-stage stage)
        (let [result (node/compute-node! opts request "create" {"COLORS_PAR_DO_TOKEN" "known-secret-token"} {:runner runner})
              error (:error result) serialized (json/generate-string result)]
          (is (= "error" (:status result)))
          (is (= code (:code error)))
          (is (= stage (:stage error)))
          (is (= risk (:infrastructure_changes error)))
          (doseq [secret ["known-secret-token" "unknown-password" "NEVER PEM BODY" "NEVER INCLUDE" "malformed-plan"]]
            (is (not (str/includes? serialized secret))))))
      (finally (remove-tree directory)))))

(deftest malformed-state-and-missing-credentials-are-actionable
  (let [[opts request] (inputs) directory (temp-dir) request (assoc request :workdir directory)]
    (try
      (let [result (node/compute-node! opts request "inspect" {}
                     {:runner (fn [argv & _] {:exit 0 :out (if (= ["tofu" "state" "pull"] argv) "{malformed-secret-state" "")})})]
        (is (= "state_unreadable" (get-in result [:error :code])))
        (is (= "state" (get-in result [:error :stage])))
        (is (= "none" (get-in result [:error :infrastructure_changes])))
        (is (not (str/includes? (json/generate-string result) "malformed-secret-state"))))
      (let [[do-opts _ do-request] (:args (first (filter #(= "digitalocean-shared" (:name %)) fixtures)))
            result (node/compute-node! (assoc do-opts :provider-backend "local")
                     (assoc (select-keys do-request [:node_id :security :network]) :workdir directory :state_filename "do.tfstate" :node_id "do-0")
                     "create" {} {:runner (fn [& _] (throw (AssertionError. "must not execute")))})]
        (is (= "missing_credentials" (get-in result [:error :code])))
        (is (= "credentials" (get-in result [:error :stage])))
        (is (= "COLORS_PAR_DO_TOKEN" (get-in result [:error :credential]))))
      (finally (remove-tree directory)))))

(deftest stderr-redaction-covers-encoded-and-nested-secrets
  (let [secret "a private+secret" nested "nested-token"
        encoded (.encodeToString (java.util.Base64/getEncoder) (.getBytes secret "UTF-8"))
        source (str "\u001b]0;hidden title\u0007\u001b[31mNo version is set for command tofu\u001b[0m\n"
                    secret " " (java.net.URLEncoder/encode secret "UTF-8") " " encoded " " nested "\n"
                    "Authorization: Bearer unknown-bearer\napi_key=unknown-assignment\n"
                    "-----BEGIN OPENSSH PRIVATE KEY-----\nPEM BODY NEVER\n-----END OPENSSH PRIVATE KEY-----\n")
        redacted (diagnostics/redact source {:credentials {:value nested}} {"SECRET_TOKEN" secret})]
    (is (str/includes? redacted "No version is set for command tofu"))
    (doseq [hidden [secret encoded nested "unknown-bearer" "unknown-assignment" "PEM BODY NEVER" "hidden title" "\u001b"]]
      (is (not (str/includes? redacted hidden))))
    (is (= "[structured output suppressed]" (diagnostics/redact "error: {\"resources\": [{\"private_key\": \"unknown\"}]}" {} {})))
    (is (= 2000 (count (diagnostics/redact (apply str (repeat 4000 "x")) {} {}))))))

(deftest unknown-multiword-secret-assignments-are-fully-suppressed
  (doseq [assignment ["password: correct horse battery staple"
                      "api_token = several words of secret data"
                      "secret_key\": \"quoted secret with spaces\" trailing private detail"]]
    (let [result (diagnostics/redact (str assignment "\nNo version is set for command tofu") {} {})]
      (is (str/includes? result "[REDACTED]"))
      (is (str/ends-with? result "\nNo version is set for command tofu"))
      (doseq [secret ["correct" "horse" "battery" "staple" "several" "words" "quoted" "trailing" "detail"]]
        (is (not (str/includes? result secret)))))))
