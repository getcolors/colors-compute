(ns io.github.getcolors.compute-node
  "One persistent SDK-owned compute unit. OpenTofu owns all remote resources."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [clojure.walk :as walk]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-local :as local]
            [io.github.getcolors.compute-gcs :as gcs]
            [io.github.getcolors.compute-request :as request]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files Path LinkOption]
           [java.net URI]
           [java.util Base64]
           [java.security MessageDigest]
           [java.math BigInteger]))

(defn- require! [condition message]
  (when-not condition (throw (ex-info message {}))))
(defn- safe? [value]
  (and (string? value) (boolean (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" value))))
(defn- nonblank? [value] (and (string? value) (not (str/blank? value))))
(defn- parse-one [text]
  (let [documents (vec (json/parsed-seq (java.io.StringReader. text) true))]
    (require! (= 1 (count documents)) "invalid OpenTofu JSON")
    (first documents)))
(defn- deep-merge [& values]
  (apply merge-with (fn [a b] (if (and (map? a) (map? b)) (deep-merge a b) b)) values))
(defn- joined [& parts] (if (= "/" (first parts)) (str "/" (str/join "/" (rest parts))) (str/join "/" (remove empty? parts))))
(defn- unit-name [profile node-id]
  (let [value (str profile "-" node-id)]
    (require! (safe? value) "combined profile and node_id must be at most 63 characters")
    value))
(def ^:private recipes
  (json/parse-string (slurp (io/resource "colors_compute/provider-recipes.json")) true))

(defn- storage [opts]
  (if (= "local" (:provider-backend opts))
    (do (require! (not-any? #(contains? opts %) [:ssh-s3-bucket :ssh-s3-region :ssh-s3-endpoint]) "local SSH keys belong to local state")
        {:kind "local" :explicit false})
  (let [kind (:provider-backend opts)
        explicit? (not (contains? #{"s3" "r2" "oci"} kind))
        _ (require! (or explicit? (not-any? #(contains? opts %) [:ssh-s3-bucket :ssh-s3-region :ssh-s3-endpoint]))
                    "SSH storage is bound to the state backend")
        backend (when (contains? #{"s3" "r2" "oci"} kind) (compute/backend-settings opts "validation.tfstate"))
        bucket (if explicit? (:ssh-s3-bucket opts) (:bucket backend))
        region (if explicit? (:ssh-s3-region opts) (:region backend))
        endpoint (if explicit? (:ssh-s3-endpoint opts) (get-in backend [:endpoints :s3]))
        credential-kind (if explicit? "s3" kind)]
    (require! (and (nonblank? bucket) (nonblank? region)) "S3 SSH storage bucket and region are required")
    (when endpoint
      (let [uri (URI. endpoint)]
        (require! (and (= "https" (.getScheme uri)) (nonblank? (.getHost uri))
                       (nil? (.getUserInfo uri)) (nil? (.getQuery uri)) (nil? (.getFragment uri))
                       (contains? #{nil "" "/"} (.getPath uri))) "invalid SSH storage endpoint")))
    {:bucket bucket :region region :endpoint endpoint :kind credential-kind :explicit explicit?})))

(defn node-plan
  "Render a single root module. request includes node_id, state_filename and SDK workdir."
  [opts node-request]
  (walk/postwalk (fn [value]
                   (when (string? value)
                     (require! (not-any? #(str/includes? value %) ["${" "%{" (str (char 0))]) "invalid compute literal"))
                   value) [opts node-request])
  (let [{:keys [node_id state_filename workdir]} node-request
        profile (:profile opts) prefix (get opts :s3-prefix "")
        _ (require! (and (safe? profile) (safe? node_id)) "profile and node_id must be safe identifiers")
        _ (require! (and (string? state_filename) (re-matches #"[A-Za-z0-9][A-Za-z0-9_.-]*\.tfstate" state_filename)) "invalid node state filename")
        _ (require! (compute/local-state-dir? workdir) "workdir must be an absolute normalized path")
        _ (require! (and (string? prefix) (or (empty? prefix) (every? #(boolean (re-matches #"[A-Za-z0-9][A-Za-z0-9_.-]*" %)) (str/split prefix #"/" -1)))) "invalid S3 prefix")
        _ (require! (and (every? #{:node_id :state_filename :workdir :security :network} (keys node-request))
                         (every? #(contains? node-request %) [:node_id :state_filename :workdir :security])) "node request contains unsupported fields")
        _ (require! (not-any? #(contains? opts %) [:ssh-key-path :ssh-private-key-path :ssh-public-key-path]) "external SSH keys are outside the single-node contract")
        provider (:provider-compute opts) recipe (get recipes (keyword provider))
        _ (require! recipe "unsupported compute provider")
        _ (require! (not-any? #(contains? opts %) (keep identity [(some-> (get-in compute/registry [:compute (keyword provider) :ssh-setting]) keyword)
                                                                (keyword (str provider "-ssh-private-key"))]))
                    "external SSH keys are outside the single-node contract")
        _ (require! (not (contains? opts :compute-role-settings)) "topology options are outside the compute node API")
        name (unit-name profile node_id)
        scoped (assoc opts :profile name)
        public-marker "colors-compute-public-key-sentinel"
        raw (-> node-request (dissoc :state_filename :workdir)
                (assoc :name name :key {:mode "managed" :public_key public-marker})
                (update :network #(or % {:mode (:network_mode recipe)})))
        shared (json/parse-string (json/generate-string (:documents (request/provider-request scoped "shared" raw))) true)
        shared-root (apply deep-merge (vals shared))
        replacements (atom {public-marker "${tls_private_key.machine.public_key_openssh}"})
        counter (atom 0)
        mark (fn mark [value]
               (cond (map? value) (into {} (map (fn [[k v]] [k (if (= k :provider) v (mark v))]) value))
                     (vector? value) (mapv mark value)
                     :else (let [token (str "colors-compute-shared-" (swap! counter inc))]
                             (swap! replacements assoc token value) token)))
        shared-values (into {} (map (fn [[k v]] [k (mark (:value v))]) (:output shared-root)))
        node (json/parse-string (json/generate-string (:documents (request/provider-request scoped "node" raw shared-values))) true)
        root (deep-merge (dissoc shared-root :output) (apply deep-merge (vals node)))
        root (walk/postwalk #(if (string? %) (get @replacements % %) %) root)
        store (storage opts)
        state-key (joined prefix profile state_filename)
        key-prefix (joined prefix profile node_id)
        remote {:private (str key-prefix "/ssh-key") :public (str key-prefix "/ssh-key.pub")}
        key-provider (cond-> {:alias "keys" :region (:region store)}
                       (:endpoint store) (assoc :endpoints {:s3 (:endpoint store)} :s3_use_path_style (not= "r2" (:kind store))
                                               :skip_credentials_validation true :skip_metadata_api_check true
                                               :skip_region_validation true :skip_requesting_account_id true)
                       true
                       (assoc :access_key "${var.keys_access_key}" :secret_key "${var.keys_secret_key}"))
        old-aws (get-in root [:provider :aws])
        root (-> root
                 (assoc-in [:terraform :required_providers :tls] {:source "hashicorp/tls" :version "4.1.0"})
                 (assoc-in [:terraform :required_providers :aws] {:source "hashicorp/aws" :version "6.31.0"})
                 (assoc-in [:provider :aws] (if old-aws [old-aws key-provider] [key-provider]))
                 (assoc-in [:resource :tls_private_key :machine] {:algorithm "ED25519"}))
        root (assoc root :variable {:keys_access_key {:type "string" :sensitive true :default nil}
                                      :keys_secret_key {:type "string" :sensitive true :default nil}})
        root (reduce (fn [root [resource-name object-key content]]
                       (assoc-in root [:resource :aws_s3_object resource-name]
                                 (cond-> {:provider "aws.keys" :bucket (:bucket store) :key object-key
                                          :content content :force_destroy false}
                                   (and (= "s3" (:kind store)) (nil? (:endpoint store))) (assoc :server_side_encryption "AES256")))) root
                     [[:ssh_private (:private remote) "${tls_private_key.machine.private_key_openssh}"]
                      [:ssh_public (:public remote) "${tls_private_key.machine.public_key_openssh}"]])
        ;; Every provider-owned resource waits for durable keys. This also orders
        ;; destruction so credentials survive all infrastructure that uses them.
        root (update root :resource
                     (fn [resources] (into {} (for [[kind instances] resources]
                       [kind (if (contains? #{:tls_private_key :aws_s3_object} kind) instances
                                 (into {} (for [[id config] instances]
                                    [id (update config :depends_on #(vec (distinct (concat % ["aws_s3_object.ssh_private" "aws_s3_object.ssh_public"]))))])))]))))
        root (-> root
                 (assoc-in [:output :compute_identity] {:value {:profile profile :node_id node_id :state_filename state_filename :provider provider}})
                 (assoc-in [:output :ssh_public_key_fingerprint] {:value "${tls_private_key.machine.public_key_fingerprint_sha256}"}))
        root (if (= "local" (:kind store))
               (let [root (-> root
                              (dissoc :variable)
                              (update :resource dissoc :aws_s3_object)
                              (assoc-in [:output :ssh_private_key] {:value "${tls_private_key.machine.private_key_openssh}" :sensitive true})
                              (assoc-in [:output :ssh_public_key] {:value "${tls_private_key.machine.public_key_openssh}"}))
                     root (if old-aws (assoc-in root [:provider :aws] old-aws)
                              (-> root (update :provider dissoc :aws) (update-in [:terraform :required_providers] dissoc :aws)))]
                 (update root :resource
                         (fn [resources] (into {} (for [[kind instances] resources]
                           [kind (if (= :tls_private_key kind) instances
                                     (into {} (for [[id config] instances]
                                       [id (update config :depends_on #(vec (distinct (conj (vec (remove #{"aws_s3_object.ssh_private" "aws_s3_object.ssh_public"} %)) "tls_private_key.machine"))))])))]))))) root)
        backend (if (= "local" (:provider-backend opts))
                  {:config {:terraform {:backend {:local {:path (joined workdir profile node_id state_filename)}}}}}
                  (compute/backend-plan opts state-key))]
    {:status "planned" :directory (joined workdir profile node_id) :state_key state-key :key_objects (if (= "local" (:kind store)) {} remote)
     :documents {"compute.tf.json" root "backend.tf.json" (:config backend)}}))

(defn- write-private! [directory filename text]
  (let [target (str (.resolve ^Path directory filename))]
    (local/prepare! target)
    (local/write-atomic! target text)
    target))

(defn build-node!
  "Persist templates in the SDK workdir. Never removes templates or init files."
  [opts node-request]
  (let [plan (node-plan opts node-request) directory (local/path (:directory plan))]
    (local/private-owned-directory! (:workdir node-request) directory)
    (doseq [filename [".terraform" ".terraform.lock.hcl" "approved.tfplan" (:state_filename node-request)]]
      (let [path (.resolve directory filename)]
        (require! (not (Files/isSymbolicLink path)) "unsafe OpenTofu working file")))
    (let [existing (str (.resolve directory "compute.tf.json"))]
      (when (= "present" (:status (local/presence existing)))
        (let [old (parse-one (slurp existing)) new (get-in plan [:documents "compute.tf.json"])]
          (require! (= (get-in old [:output :compute_identity :value])
                       (get-in new [:output :compute_identity :value]))
                    "compute identity changed; use a separate node directory for migration")))
      (let [existing-backend (str (.resolve directory "backend.tf.json"))]
        (when (= "present" (:status (local/presence existing-backend)))
          (require! (= (parse-one (slurp existing-backend)) (get-in plan [:documents "backend.tf.json"]))
                    "compute backend changed; explicit state migration is required"))))
    (doseq [[filename document] (:documents plan)]
      (write-private! directory filename (json/generate-string document)))
    (assoc plan :status "built")))

(defn- safe-environment [environment]
  (into {} (remove (fn [[key _]] (some #(str/starts-with? key %) ["TF_" "TOFU_" "COLORS_PAR_"])) environment)))
(defn- execute! [runner directory environment arguments timeout]
  (let [result (runner arguments directory environment timeout)]
    (require! (= 0 (:exit result)) "compute command failed")
    (:out result)))
(defn- storage-environment [store environment]
  (let [base (merge (safe-environment environment) {"AWS_PAGER" "" "AWS_CLI_AUTO_PROMPT" "off"})
        prefix (cond (:explicit store) "SSH_S3" (= "r2" (:kind store)) "R2" (= "oci" (:kind store)) "OCI")
        access (when prefix (get environment (str "COLORS_PAR_" prefix "_ACCESS_KEY_ID")))
        secret (when prefix (get environment (str "COLORS_PAR_" prefix "_SECRET_ACCESS_KEY")))]
    (when (and prefix (not (:explicit store)))
      (require! (and (nonblank? access) (nonblank? secret)) "required SSH storage credentials are not set"))
    (require! (= (boolean access) (boolean secret)) "both SSH storage credentials are required")
    (if access
      (merge (into {} (remove #(and (str/starts-with? (key %) "AWS_") (not= "AWS_CA_BUNDLE" (key %))) base))
             {"AWS_ACCESS_KEY_ID" access "AWS_SECRET_ACCESS_KEY" secret "AWS_PAGER" "" "AWS_CLI_AUTO_PROMPT" "off"
              "AWS_REQUEST_CHECKSUM_CALCULATION" "when_required" "AWS_RESPONSE_CHECKSUM_VALIDATION" "when_required"}) base)))
(defn- object-presence! [store object-key directory environment runner]
  (let [file (write-private! (local/path directory) ".remote-presence" "")]
    (try
      (let [result (runner (into ["aws" "s3api" "get-object" "--bucket" (:bucket store) "--key" object-key "--region" (:region store) "--no-cli-pager"]
                                (concat (when (:endpoint store) ["--endpoint-url" (:endpoint store)]) [file]))
                           directory (storage-environment store environment) 120000)]
        (cond (= 0 (:exit result)) "present"
              (and (string? (:err result))
                   (re-find #"^\s*(?:aws: \[ERROR\]: )?An error occurred \(NoSuchKey\) when calling the GetObject operation(?: \(reached max retries: [0-9]+\))?:" (:err result))) "absent"
              :else (throw (ex-info "could not read remote object; refusing mutation" {}))))
      (finally (Files/deleteIfExists (local/path file))))))
(defn- valid-state! [text identity]
  (let [state (parse-one text)]
    (require! (runtime/valid-state? state) "invalid compute state")
    (if (seq (:resources state))
      (do (require! (= identity (get-in state [:outputs :compute_identity :value])) "state identity mismatch; recover or delete the original node first")
          (require! (= (:provider identity) (get-in state [:outputs :params :value :provider])) "state provider mismatch; recover or delete the original node first"))
      (require! (empty? (:outputs state)) "state without resources still has outputs; explicit recovery required"))
    state))
(defn- guarded-plan! [text operation]
  (let [plan (parse-one text) permitted (if (= operation "delete") #{"no-op" "read" "delete"} #{"no-op" "read" "create" "update"})]
    (require! (and (map? plan) (nonblank? (:format_version plan)) (map? (:planned_values plan))
                   (vector? (get plan :resource_changes []))
                   (every? (fn [r] (let [actions (get-in r [:change :actions])]
                                     (and (vector? actions) (= 1 (count actions)) (contains? permitted (first actions)))))
                           (get plan :resource_changes []))) "compute plan requires an unauthorized replacement or deletion")))

(defn- normalized [params environment]
  (require! (map? params) "invalid compute outputs")
  (let [templates (get (json/parse-string (slurp (io/resource "colors_compute/templates.json")) true) (keyword (:provider params)))
        allowed (into #{:ssh_identity_file} (mapcat (fn [[stage documents]]
                                                    (when (str/starts-with? (name stage) "node")
                                                      (mapcat #(keys (get-in % [:output :params :value])) (vals documents)))) templates))
        _ (require! (every? allowed (keys params)) "unexpected node outputs")
        result params
        text (json/generate-string result)]
    (doseq [field [:provider :name :ip :user :sudoer]]
      (require! (nonblank? (get result field)) "incomplete compute outputs"))
    (require! (and (not (str/includes? text "-----BEGIN "))
                   (not-any? #(str/includes? (str/lower-case text) %) ["\"private_key\"" "\"private_key_openssh\"" "\"secret_key\"" "\"access_key\""])) "secret compute output")
    (doseq [[variable secret] environment
            :when (and (nonblank? secret) (or (str/starts-with? variable "COLORS_PAR_") (re-find #"SECRET|TOKEN|PASSWORD|ACCESS_KEY|API_KEY" variable)))]
      (let [encoded (json/generate-string secret)]
        (require! (not (or (str/includes? text secret) (str/includes? text (subs encoded 1 (dec (count encoded)))))) "credential compute output")))
    result))

(defn- prepare-access! [plan state environment runner]
  (let [directory (local/path (:directory plan)) store (:storage plan)
        files {:private "ssh-key" :public "ssh-key.pub"}
        cli-env (storage-environment store environment)]
    (try
      ;; Never fall back to a local key, even when only one download fails.
      (doseq [[kind filename] files]
        (if (= "local" (:kind store))
          (let [value (get-in state [:outputs (if (= kind :private) :ssh_private_key :ssh_public_key) :value])]
            (require! (nonblank? value) "local state SSH key is unavailable")
            (when (= kind :private)
              (require! (true? (get-in state [:outputs :ssh_private_key :sensitive])) "local private key output must be sensitive"))
            (write-private! directory (str filename ".download") value))
          (let [path (write-private! directory (str filename ".download") "")]
            (execute! runner (:directory plan) cli-env
                    (into ["aws" "s3api" "get-object" "--bucket" (:bucket store) "--key" (get-in plan [:key_objects kind])
                           "--region" (:region store) "--no-cli-pager"]
                          (concat (when (:endpoint store) ["--endpoint-url" (:endpoint store)]) [path])) 120000))))
      (let [public (str/trim (slurp (.toFile (.resolve directory "ssh-key.pub.download"))))
            derived (str/trim (execute! runner (:directory plan) (safe-environment environment)
                                       ["ssh-keygen" "-y" "-f" (str (.resolve directory "ssh-key.download"))] 120000))
            expected (some #(when (= "tls_private_key" (:type %)) (get-in % [:instances 0 :attributes :public_key_openssh])) (:resources state))
            normalize #(str/join " " (take 2 (str/split (str/trim %) #"\s+")))
            parts (str/split public #"\s+")
            _ (require! (and (<= 2 (count parts)) (= "ssh-ed25519" (first parts))) "invalid remote SSH public key")
            fingerprint (str "SHA256:" (.encodeToString (.withoutPadding (Base64/getEncoder))
                                                        (.digest (MessageDigest/getInstance "SHA-256") (.decode (Base64/getDecoder) ^String (second parts)))))]
        (require! (and (nonblank? expected) (= (normalize public) (normalize derived) (normalize expected))
                       (= fingerprint (get-in state [:outputs :ssh_public_key_fingerprint :value]))) "remote SSH keypair does not match node state")
        (doseq [filename (vals files)]
          (write-private! directory filename (slurp (.toFile (.resolve directory (str filename ".download"))))))
        {:private (str (.resolve directory "ssh-key")) :public (str (.resolve directory "ssh-key.pub"))})
      (catch Exception error
        (doseq [filename (vals files)] (Files/deleteIfExists (.resolve directory filename)))
        (throw error))
      (finally (doseq [filename (vals files)] (Files/deleteIfExists (.resolve directory (str filename ".download"))))))))

(defn- compute-node*
  "Run one unit using native OpenTofu locking. deps accepts :runner for testing."
  ([opts node-request] (compute-node* opts node-request "create" (into {} (System/getenv)) {}))
  ([opts node-request operation] (compute-node* opts node-request operation (into {} (System/getenv)) {}))
  ([opts node-request operation environment] (compute-node* opts node-request operation environment {}))
  ([opts node-request operation environment deps]
   (require! (contains? #{"create" "delete" "inspect" "prepare-access"} operation) "invalid compute operation")
   (require! (or (not (contains? opts :compute-require-existing-state)) (boolean? (:compute-require-existing-state opts))) "invalid existing-state requirement")
   (require! (or (not= "delete" operation) (false? (:compute-prevent-destroy opts))) "compute deletion requires compute-prevent-destroy=false")
   (let [plan (assoc (build-node! opts node-request) :storage (storage opts)) directory (local/path (:directory plan))
         identity {:profile (:profile opts) :node_id (:node_id node-request) :state_filename (:state_filename node-request) :provider (:provider-compute opts)}
         runner (get deps :runner runtime/run-command)
         backend (assoc (compute/backend-plan opts (:state_key plan)) :config (get-in plan [:documents "backend.tf.json"]))
         credentials (into {} (for [[variable option] (:credential_bindings backend)]
                                (let [value (get environment variable)]
                                  (require! (nonblank? value) (str "required credential is not set: " variable)) [option value])))
         provider-env (into {} (for [[key target] (get-in compute/registry [:compute (keyword (:provider-compute opts)) :tofu-env])]
                                 (let [variable (str "COLORS_PAR_" (str/upper-case (str/replace (name key) "-" "_"))) value (get environment variable)]
                                   (require! (nonblank? value) (str "required credential is not set: " variable)) [target value])))
         env (merge (safe-environment environment) provider-env {"TF_IN_AUTOMATION" "1" "TF_INPUT" "0" "TF_WORKSPACE" "default"})
         kind (get-in plan [:storage :kind])
         env (if (or (contains? #{"r2" "oci"} kind) (:explicit (:storage plan)))
               (let [prefix (if (:explicit (:storage plan)) "SSH_S3" (if (= kind "r2") "R2" "OCI"))
                     access (get environment (str "COLORS_PAR_" prefix "_ACCESS_KEY_ID"))
                     secret (get environment (str "COLORS_PAR_" prefix "_SECRET_ACCESS_KEY"))]
                 (require! (or (and (:explicit (:storage plan)) (nil? access) (nil? secret))
                               (and (nonblank? access) (nonblank? secret))) "required SSH storage credentials are not set")
                 (if access (assoc env "TF_VAR_keys_access_key" access "TF_VAR_keys_secret_key" secret) env)) env)
         credential-file (write-private! directory "credentials.tfbackend.json" (json/generate-string credentials))
         command #(execute! runner (:directory plan) env (into ["tofu"] %) 1800000)]
     (try
       (command ["init" "-input=false" "-no-color" "-reconfigure" (str "-backend-config=" credential-file)])
       (let [pulled (runner ["tofu" "state" "pull"] (:directory plan) env 120000)
             needs-presence? (or (not= 0 (:exit pulled)) (not (nonblank? (:out pulled))))
             observed (when needs-presence?
                        (case (:provider-backend opts)
                          "local" (:status (local/presence (get-in backend [:config :terraform :backend :local :path])))
                          ("s3" "r2" "oci")
                          (object-presence! (storage (dissoc opts :ssh-s3-bucket :ssh-s3-region :ssh-s3-endpoint))
                                            (:state_key plan) (:directory plan) environment runner)
                          "gcs" (let [request ((get deps :gcs-client gcs/client) environment runner)]
                                  (if (request "GET" (gcs/object-path (:gcs-bucket opts) (str (:state_key plan) "/default.tfstate")) nil {}) "present" "absent"))
                          "error"))
             _ (require! (or (not needs-presence?) (= "absent" observed)) "could not read compute state; refusing mutation")
             before (when-not needs-presence? (valid-state! (:out pulled) identity))
             _ (require! (or before (and (= operation "create") (not (:compute-require-existing-state opts)))) "compute state is required")
             _ (when (and (= operation "create") (= "local" (:provider-backend opts)) (nil? before))
                 (doseq [filename ["ssh-key" "ssh-key.pub"]]
                   (require! (= "absent" (:status (local/presence (str (.resolve directory filename)))))
                             "local SSH copies exist without owned state; recover state before creation")))
             _ (when (= operation "create")
                 (doseq [[kind object-key] (:key_objects plan)
                         :let [resource-name (str "ssh_" (name kind))
                               owned (some #(and (= "managed" (:mode %)) (= "aws_s3_object" (:type %)) (= resource-name (:name %))
                                                 (some (fn [instance] (and (= object-key (get-in instance [:attributes :key]))
                                                                          (= (:bucket (:storage plan)) (get-in instance [:attributes :bucket])))) (:instances %))) (:resources before))]
                         :when (not owned)]
                   (require! (= "absent" (object-presence! (:storage plan) object-key (:directory plan) environment runner))
                             "remote SSH keys exist without owned state; recover state before creation")))]
         (if (contains? #{"inspect" "prepare-access"} operation)
           (if (and (= "inspect" operation) (empty? (:resources before)) (empty? (:outputs before)))
             {:status "destroyed" :directory (:directory plan)}
           (do (require! (seq (:resources before)) "compute node does not exist")
               (let [params (normalized (get-in before [:outputs :params :value]) environment)]
                 {:status "ready" :directory (:directory plan)
                  :params (cond-> params (= operation "prepare-access") (assoc :ssh_identity_file (:private (prepare-access! plan before environment runner))))})))
           (let [plan-path (str (.resolve directory "approved.tfplan"))]
             (command (cond-> ["plan" "-input=false" "-no-color" (str "-out=" plan-path)] (= operation "delete") (conj "-destroy")))
             (guarded-plan! (command ["show" "-json" plan-path]) operation)
             (command ["apply" "-input=false" "-no-color" plan-path])
             (let [after (valid-state! (command ["state" "pull"]) identity)]
               (if (= operation "delete")
                 (do (require! (empty? (:resources after)) "compute deletion is incomplete")
                     (doseq [filename ["ssh-key" "ssh-key.pub"]] (Files/deleteIfExists (.resolve directory filename)))
                     {:status "destroyed" :directory (:directory plan)})
                 (let [params (normalized (get-in after [:outputs :params :value]) environment)]
                   {:status "ready" :directory (:directory plan)
                    :params (assoc params :ssh_identity_file (:private (prepare-access! plan after environment runner)))}))))))
       (finally (Files/deleteIfExists (local/path credential-file)))))))

(defn compute-node!
  "Execute one node; errors are redacted, cancellation propagates."
  ([opts node-request] (compute-node! opts node-request "create" (into {} (System/getenv)) {}))
  ([opts node-request operation] (compute-node! opts node-request operation (into {} (System/getenv)) {}))
  ([opts node-request operation environment] (compute-node! opts node-request operation environment {}))
  ([opts node-request operation environment deps]
   (try
     (if (= "build" operation) (build-node! opts node-request)
         (compute-node* opts node-request operation environment deps))
     (catch InterruptedException error (throw error))
     (catch Exception _ {:status "error"}))))
