(ns io.github.getcolors.compute-node
  "One persistent SDK-owned compute unit. OpenTofu owns all remote resources."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [clojure.walk :as walk]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-local :as local]
            [io.github.getcolors.compute-gcs :as gcs]
            [io.github.getcolors.compute-diagnostics :as diagnostics]
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

(def ^:private registrations {"aws" "aws_key_pair" "digitalocean" "digitalocean_ssh_key" "hcloud" "hcloud_ssh_key" "vultr" "vultr_ssh_key"})
(defn- public-identity [value]
  (require! (and (map? value) (every? #{:reference :public_key :fingerprint :status} (keys value)) (nonblank? (:reference value))) "SSH resource reference required")
  (let [parts (str/split (or (:public_key value) "") #"\s+")
        _ (require! (and (= 2 (count parts)) (= "ssh-ed25519" (first parts))) "ED25519 public identity required")
        blob (.decode (Base64/getDecoder) ^String (second parts))
        header (.decode (Base64/getDecoder) "AAAAC3NzaC1lZDI1NTE5AAAAIA==")
        _ (require! (and (= 51 (count blob)) (= (seq header) (take 19 blob))) "invalid ED25519 identity")
        fingerprint (str "SHA256:" (.encodeToString (.withoutPadding (Base64/getEncoder)) (.digest (MessageDigest/getInstance "SHA-256") blob)))]
    (require! (= fingerprint (:fingerprint value)) "SSH fingerprint mismatch") value))
(defn- registration-identity [provider request identity]
  (let [value (:ssh_registration request)]
    (if (contains? registrations provider)
      (do (require! (and (map? value) (= "ready" (:status value)) (= provider (:provider value)) (= (:reference identity) (:ssh_resource_reference value)) (= (:fingerprint identity) (:fingerprint value)) (nonblank? (:id value)) (nonblank? (:reference value))) "matching SSH registration required") value)
      (do (require! (nil? value) "provider consumes public identity directly") nil))))

(defn node-plan
  "Render a single root module. request includes node_id, state_filename and SDK workdir."
  [opts node-request]
  (diagnostics/stage! "validate")
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
        _ (require! (and (every? #{:node_id :state_filename :workdir :security :network :ssh_resource :ssh_registration} (keys node-request))
                         (every? #(contains? node-request %) [:node_id :state_filename :workdir :security :ssh_resource])) "node request contains unsupported fields")
        _ (require! (not-any? #(contains? opts %) [:ssh-key-path :ssh-private-key-path :ssh-public-key-path]) "external SSH keys are outside the single-node contract")
        provider (:provider-compute opts) recipe (get recipes (keyword provider))
        _ (require! recipe "unsupported compute provider")
        _ (require! (not-any? #(contains? opts %) (keep identity [(some-> (get-in compute/registry [:compute (keyword provider) :ssh-setting]) keyword)
                                                                (keyword (str provider "-ssh-private-key"))]))
                    "external SSH keys are outside the single-node contract")
        _ (require! (not (contains? opts :compute-role-settings)) "topology options are outside the compute node API")
        identity (public-identity (:ssh_resource node-request))
        registration (registration-identity provider node-request identity)
        name (unit-name profile node_id)
        scoped (assoc opts :profile name)
        public-marker "colors-compute-public-key-sentinel"
        raw (-> node-request (dissoc :state_filename :workdir :ssh_resource :ssh_registration)
                (assoc :name name :key {:mode "managed" :public_key public-marker})
                (update :network #(or % {:mode (:network_mode recipe)})))
        shared (json/parse-string (json/generate-string (:documents (request/provider-request scoped "shared" raw))) true)
        shared-root (apply deep-merge (vals shared))
        replacements (atom (merge {public-marker (:public_key identity)} (when registration (into {} (for [field ["id" "key_name"]] [(str "${" (get registrations provider) ".machine." field "}") (:id registration)])))))
        counter (atom 0)
        mark (fn mark [value]
               (cond (map? value) (into {} (map (fn [[k v]] [k (if (= k :provider) v (mark v))]) value))
                     (vector? value) (mapv mark value)
                     :else (let [token (str "colors-compute-shared-" (swap! counter inc))]
                             (swap! replacements assoc token value) token)))
        shared-values (into {} (map (fn [[k v]] [k (mark (:value v))]) (:output shared-root)))
        node (json/parse-string (json/generate-string (:documents (request/provider-request scoped "node" raw shared-values))) true)
        root (deep-merge (dissoc shared-root :output) (apply deep-merge (vals node)))
        root (walk/postwalk #(loop [v %] (if (and (string? v) (contains? @replacements v)) (recur (get @replacements v)) v)) root)
        state-key (joined prefix profile state_filename)
        root (cond-> root registration (update :resource dissoc (keyword (get registrations provider))))
        root (assoc-in root [:output :compute_identity] {:value {:profile profile :node_id node_id :state_filename state_filename :provider provider :ssh_resource_reference (:reference identity) :ssh_fingerprint (:fingerprint identity)}})
        backend (if (= "local" (:provider-backend opts))
                  {:config {:terraform {:backend {:local {:path (joined workdir profile node_id state_filename)}}}}}
                  (compute/backend-plan opts state-key))]
    {:status "planned" :directory (joined workdir profile node_id) :state_key state-key :key_objects {}
     :documents {"compute.tf.json" root "backend.tf.json" (:config backend)}}))

(defn- write-private! [directory filename text]
  (let [target (str (.resolve ^Path directory filename))]
    (local/prepare! target)
    (local/write-atomic! target text)
    target))

(def ^:dynamic *planner* node-plan)
(def ^:dynamic *normalizer* nil)

(defn build-node!
  "Persist templates in the SDK workdir. Never removes templates or init files."
  [opts node-request]
  (let [plan (*planner* opts node-request) directory (local/path (:directory plan))]
    (diagnostics/stage! "build")
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
    (when-not (= 0 (:exit result)) (diagnostics/command-error!))
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
              :else (diagnostics/command-error! "state_unreadable")))
      (finally (diagnostics/cleanup! #(Files/deleteIfExists (local/path file)))))))
(defn- uninitialized-envelope? [text]
  ;; OpenTofu may report success with a synthetic snapshot before any backend
  ;; state exists. This is only a reason to probe; it never proves absence.
  (try
    (let [state (parse-one text)]
      (and (map? state)
           (= #{:version :terraform_version :serial :lineage :outputs :resources :check_results} (set (keys state)))
           (= 4 (:version state)) (= 0 (:serial state)) (= "" (:lineage state))
           (nonblank? (:terraform_version state)) (= {} (:outputs state)) (= [] (:resources state))
           (nil? (:check_results state))))
    (catch Exception _ false)))

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
        allowed (into #{} (mapcat (fn [[stage documents]]
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

(defn- compute-node*
  "Run one unit using native OpenTofu locking. deps accepts :runner for testing."
  ([opts node-request] (compute-node* opts node-request "create" (into {} (System/getenv)) {}))
  ([opts node-request operation] (compute-node* opts node-request operation (into {} (System/getenv)) {}))
  ([opts node-request operation environment] (compute-node* opts node-request operation environment {}))
  ([opts node-request operation environment deps]
   (require! (contains? #{"create" "delete" "inspect"} operation) "invalid compute operation")
   (require! (or (not (contains? opts :compute-require-existing-state)) (boolean? (:compute-require-existing-state opts))) "invalid existing-state requirement")
   (require! (or (not= "delete" operation) (false? (:compute-prevent-destroy opts))) "compute deletion requires compute-prevent-destroy=false")
   (let [plan (build-node! opts node-request) directory (local/path (:directory plan))
         identity (get-in plan [:documents "compute.tf.json" :output :compute_identity :value])
         runner (diagnostics/wrap-runner (get deps :runner runtime/run-command))
         _ (diagnostics/stage! "credentials")
         backend (assoc (compute/backend-plan opts (:state_key plan)) :config (get-in plan [:documents "backend.tf.json"]))
         credentials (into {} (for [[variable option] (:credential_bindings backend)]
                                (let [value (get environment variable)]
                                  (require! (nonblank? value) (str "required credential is not set: " variable)) [option value])))
         provider-env (into {} (for [[key target] (get-in compute/registry [:compute (keyword (:provider-compute opts)) :tofu-env])]
                                 (let [variable (str "COLORS_PAR_" (str/upper-case (str/replace (name key) "-" "_"))) value (get environment variable)]
                                   (require! (nonblank? value) (str "required credential is not set: " variable)) [target value])))
         env (merge (safe-environment environment) provider-env {"TF_IN_AUTOMATION" "1" "TF_INPUT" "0" "TF_WORKSPACE" "default"})
         credential-file (write-private! directory "credentials.tfbackend.json" (json/generate-string credentials))
         command (fn [arguments]
                   (diagnostics/stage! (case (first arguments) "init" "init" "state" "state" "plan" "plan" "show" "plan-validation" "apply" "apply" "validate"))
                   (when (= "apply" (first arguments)) (diagnostics/mutation!))
                   (execute! runner (:directory plan) env (into ["tofu"] arguments) 1800000))]
     (try
       (command ["init" "-input=false" "-no-color" "-reconfigure" (str "-backend-config=" credential-file)])
       (diagnostics/stage! "state")
       (let [pulled (runner ["tofu" "state" "pull"] (:directory plan) env 120000)
             needs-presence? (or (not= 0 (:exit pulled)) (not (nonblank? (:out pulled)))
                                 (uninitialized-envelope? (:out pulled)))
             observed (when needs-presence?
                        (case (:provider-backend opts)
                          "local" (:status (local/presence (get-in backend [:config :terraform :backend :local :path])))
                          ("s3" "r2" "oci")
                          (object-presence! (storage (dissoc opts :ssh-s3-bucket :ssh-s3-region :ssh-s3-endpoint))
                                            (:state_key plan) (:directory plan) environment runner)
                          "gcs" (let [request ((get deps :gcs-client gcs/client) environment runner)]
                                  (if (request "GET" (gcs/object-path (:gcs-bucket opts) (str (:state_key plan) "/default.tfstate")) nil {}) "present" "absent"))
                          "error"))
             _ (when-not (or (not needs-presence?) (= "absent" observed)) (diagnostics/command-error! "state_unreadable"))
             before (when-not needs-presence? (valid-state! (:out pulled) identity))
             _ (require! (or before (and (= operation "create") (not (:compute-require-existing-state opts)))) "compute state is required")
]
         (if (contains? #{"inspect"} operation)
           (if (and (= "inspect" operation) (empty? (:resources before)) (empty? (:outputs before)))
             {:status "destroyed" :directory (:directory plan)}
           (do (require! (seq (:resources before)) "compute node does not exist")
               (let [params ((or *normalizer* normalized) (get-in before [:outputs :params :value]) environment)]
                 {:status "ready" :directory (:directory plan)
                  :params params})))
           (let [plan-path (str (.resolve directory "approved.tfplan"))]
             (command (cond-> ["plan" "-input=false" "-no-color" (str "-out=" plan-path)] (= operation "delete") (conj "-destroy")))
             (guarded-plan! (command ["show" "-json" plan-path]) operation)
             (command ["apply" "-input=false" "-no-color" plan-path])
             (let [after (valid-state! (command ["state" "pull"]) identity)]
               (if (= operation "delete")
                 (do (require! (empty? (:resources after)) "compute deletion is incomplete")
                     {:status "destroyed" :directory (:directory plan)})
                 (let [params ((or *normalizer* normalized) (get-in after [:outputs :params :value]) environment)]
                   {:status "ready" :directory (:directory plan)
                    :params params}))))))
       (finally (diagnostics/cleanup! #(Files/deleteIfExists (local/path credential-file))))))))

(defn compute-node!
  "Execute one node; errors are redacted, cancellation propagates."
  ([opts node-request] (compute-node! opts node-request "create" (into {} (System/getenv)) {}))
  ([opts node-request operation] (compute-node! opts node-request operation (into {} (System/getenv)) {}))
  ([opts node-request operation environment] (compute-node! opts node-request operation environment {}))
  ([opts node-request operation environment deps]
   (binding [diagnostics/*context* (atom {:stage "validate" :infrastructure_changes "none" :opts opts :source environment})]
     (try
       (if (= "build" operation) (build-node! opts node-request)
           (compute-node* opts node-request operation environment deps))
       (catch InterruptedException error (throw error))
       (catch Exception error (diagnostics/failure error))))))


(defn- registration-request [request]
  (require! (and (= #{:name :workdir :state_filename :ssh_resource} (set (keys request))) (safe? (:name request))) "invalid SSH registration request")
  {:node_id (str "registration-" (:name request)) :workdir (:workdir request) :state_filename (:state_filename request) :ssh_resource (:ssh_resource request)})
(defn- registration-plan* [opts internal]
  (let [provider (:provider-compute opts) kind (get registrations provider)
        _ (require! kind "provider needs no SSH registration")
        identity (public-identity (:ssh_resource internal)) name (:node_id internal)
        _ (require! (and (safe? (:profile opts)) (safe? name) (safe? (str (:profile opts) "-" name)) (compute/local-state-dir? (:workdir internal)) (string? (:state_filename internal)) (re-matches #"[A-Za-z0-9][A-Za-z0-9_.-]*\.tfstate" (:state_filename internal))) "invalid registration identity")
        _ (walk/postwalk (fn [v] (when (string? v) (require! (not-any? #(str/includes? v %) ["${" "%{" (str (char 0))]) "invalid registration literal")) v) [opts internal])
        prefix (get opts :s3-prefix "")
        _ (require! (and (string? prefix) (or (empty? prefix) (every? #(re-matches #"[A-Za-z0-9][A-Za-z0-9_.-]*" %) (str/split prefix #"/" -1)))) "invalid S3 prefix")
        state-key (joined prefix (:profile opts) (:state_filename internal))
        directory (joined (:workdir internal) (:profile opts) name)
        documents (get-in (json/parse-string (slurp (io/resource "colors_compute/templates.json")) true) [(keyword provider) :shared-keygen])
        root (apply deep-merge (map #(select-keys % [:terraform :provider]) (vals documents)))
        root (if (= provider "aws") (do (require! (nonblank? (:aws-region opts)) "AWS region required") (assoc-in root [:provider :aws :region] (:aws-region opts))) root)
        root (assoc-in root [:output :compute_identity :value] {:profile (:profile opts) :node_id name :state_filename (:state_filename internal) :provider provider :ssh_resource_reference (:reference identity) :ssh_fingerprint (:fingerprint identity)})
        backend (if (= "local" (:provider-backend opts)) {:terraform {:backend {:local {:path (joined directory (:state_filename internal))}}}} (:config (compute/backend-plan opts state-key)))
        plan {:status "planned" :directory directory :state_key state-key :key_objects {} :documents {"backend.tf.json" backend}}
        resource {(if (= provider "aws") :key_name :name) (str (:profile opts) "-" name)
                  (if (= provider "vultr") :ssh_key :public_key) (:public_key identity)
                  :lifecycle {:prevent_destroy (get opts :compute-prevent-destroy true)}}
        root (-> root (dissoc :data :locals)
                 (assoc :resource {(keyword kind) {:machine resource}})
                 (assoc :output {:compute_identity {:value (assoc (get-in root [:output :compute_identity :value]) :kind "ssh-registration" :provider_scope (if (= provider "aws") (:aws-region opts) provider))}
                                 :params {:value {:provider provider :node_id name :id (str "${tostring(" kind ".machine." (if (= provider "aws") "key_name" "id") ")}")}}}))]
    (assoc-in plan [:documents "compute.tf.json"] root)))
(defn registration-plan [opts request] (registration-plan* opts (registration-request request)))
(defn build-registration! [opts request]
  (binding [*planner* registration-plan*] (build-node! opts (registration-request request))))
(defn compute-registration!
  ([opts request] (compute-registration! opts request "create" (into {} (System/getenv)) {}))
  ([opts request operation] (compute-registration! opts request operation (into {} (System/getenv)) {}))
  ([opts request operation environment] (compute-registration! opts request operation environment {}))
  ([opts request operation environment deps]
   (binding [*planner* registration-plan*
             *normalizer* (fn [params _] (require! (and (map? params) (every? #{:provider :node_id :id} (keys params)) (nonblank? (:id params))) "invalid registration outputs") params)]
     (let [result (compute-node! opts (registration-request request) operation environment deps)]
       (if (= "ready" (:status result))
         (merge (dissoc result :params) {:reference (:state_key (registration-plan opts request)) :provider (:provider-compute opts) :ssh_resource_reference (get-in request [:ssh_resource :reference]) :fingerprint (get-in request [:ssh_resource :fingerprint]) :id (get-in result [:params :id])}) result)))))
