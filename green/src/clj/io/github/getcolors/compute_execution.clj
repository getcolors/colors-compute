(ns io.github.getcolors.compute-execution
  "Internal guarded execution: caller must first commit a schema-2 attempt."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files]))
(defn- key-text [key] (if (keyword? key) (subs (str key) 1) key))
(defn- safe? [value] (and (string? value) (boolean (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" value))))
(defn- nonblank? [value] (and (string? value) (not (str/blank? value))))
(defn- missing? [value] (or (not (nonblank? value)) (= "REPLACE_ME" (str/upper-case (str/trim value)))))
(defn- require-valid [condition] (when-not condition (throw (ex-info "invalid execution" {}))))
(defn- state-key? [opts key]
  (and (safe? (:profile opts)) (string? key)
       (or (= key (str (:profile opts) "/compute/shared.tfstate"))
           (let [prefix (str (:profile opts) "/compute/nodes/")]
             (and (str/starts-with? key prefix) (str/ends-with? key ".tfstate")
                  (safe? (subs key (count prefix) (- (count key) 8))))))))
(defn state-presence
  ([opts key] (state-presence opts key (into {} (System/getenv)) runtime/run-command))
  ([opts key environment] (state-presence opts key environment runtime/run-command))
  ([opts key environment runner] (state-presence opts key environment runner false))
  ([opts key environment runner legacy]
   (try
     (require-valid (and (map? opts) (or (state-key? opts key)
                                        (and (true? legacy) (safe? (:profile opts)) (string? key)
                                             (re-matches (re-pattern (str (java.util.regex.Pattern/quote (:profile opts)) "/[A-Za-z0-9][A-Za-z0-9_-]{0,62}\\.tfstate")) key)))))
     (journal/backend-session opts environment
       (fn [directory env credentials settings]
         (let [body (journal/private-file! directory "state.json" "")
               argv (into ["aws" "s3api" "get-object" "--bucket" (:bucket settings) "--key" key body
                           "--region" (:region settings) "--output" "json" "--no-cli-pager"]
                          (when (= "r2" (:kind settings)) ["--endpoint-url" (:endpoint settings)]))]
           (require-valid (not (journal/bound-secret? argv credentials)))
           (let [result (runner argv (str directory) env 120000)]
             (if (= 0 (:exit result))
               (let [etag (:ETag (journal/parse-one (:out result)))]
                 (if (and (nonblank? etag) (not (journal/bound-secret? etag credentials))) {:status "present"} {:status "error"}))
               {:status (if (= "NoSuchKey" (journal/service-code result "GetObject")) "absent" "error")})))))
     (catch InterruptedException error (throw error))
     (catch Exception _ {:status "error"}))))
(defn- state [output]
  (let [document (journal/parse-one output) out (get-in document [:outputs :params])
        params (if (contains? (:outputs document) :params) (when (map? out) (:value out)) {})]
    (require-valid (and (runtime/valid-state? document) (map? params)))
    {:document document :params params}))
(defn- empty-state? [document] (and (empty? (:resources document)) (empty? (:outputs document))))
(defn- valid-plan? [text operation]
  (try
    (let [plan (journal/parse-one text) changes (get plan :resource_changes [])
          permitted (if (= operation "create") #{"no-op" "read" "create" "update"} #{"no-op" "read" "delete"})]
      (and (map? plan) (nonblank? (:format_version plan)) (map? (:planned_values plan)) (vector? changes)
           (every? (fn [resource]
                     (let [actions (get-in resource [:change :actions])]
                       (and (map? resource) (map? (:change resource)) (vector? actions) (= 1 (count actions)) (contains? permitted (first actions))))) changes)))
    (catch Exception _ false)))
(def ^:private templates (json/parse-string (slurp (io/resource "colors_compute/templates.json")) true))
(defn- valid-documents? [documents provider]
  (let [all (mapcat vals (vals (get templates (keyword provider))))
        specs (apply merge (map #(get-in % [:terraform :required_providers]) all))
        provider-fields (into {} (for [doc all [provider config] (:provider doc)] [provider (set (keys config))]))
        resource-types (into {} (for [kind [:resource :data]] [kind (set (mapcat #(keys (get % kind)) all))]))]
    (and (map? documents) (seq documents) (seq all)
         (every? (fn [[filename doc]]
                   (let [filename (key-text filename) terraform (get doc :terraform {}) providers (get doc :provider {})
                         required (get terraform :required_providers {}) text (json/generate-string doc)]
                     (and (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]*\.tf\.json" filename) (not= filename "backend.tf.json")
                          (map? doc) (every? #{:terraform :provider :resource :data :locals :output} (keys doc))
                          (map? terraform) (not (contains? terraform :backend)) (map? providers) (map? required)
                          (every? (fn [[provider spec]] (and (contains? specs provider) (= spec (get specs provider)))) required)
                          (every? (fn [[provider config]] (and (contains? provider-fields provider) (map? config)
                                                                (every? (get provider-fields provider) (keys config)))) providers)
                          (every? (fn [kind] (and (map? (get doc kind {})) (every? (get resource-types kind) (keys (get doc kind {}))))) [:resource :data])
                          (not-any? #(str/includes? text %) ["-----BEGIN " "\"private_key\"" "\"provisioner\""])))) documents))))
(defn- converge* [opts key documents operation presence environment runner]
  (require-valid (and (map? opts) (state-key? opts key) (contains? #{"create" "delete"} operation)
                      (map? presence) (= #{:status} (set (keys presence))) (contains? #{"present" "absent"} (:status presence))))
  (let [documents (json/parse-string-strict (json/generate-string documents) true)
        protect (get opts :compute-prevent-destroy true) provider (:provider-compute opts)]
    (require-valid (and (boolean? protect) (or (= operation "create") (not protect))
                        (string? provider) (contains? (:compute compute/registry) (keyword provider)) (valid-documents? documents provider)))
    (let [plan (compute/backend-plan opts key)]
      (if (and (= operation "delete") (= "absent" (:status presence))) {:status "destroyed"}
          (let [credentials (into {} (for [[variable option] (:credential_bindings plan)]
                                       (let [value (get environment variable)] (require-valid (not (missing? value))) [option value])))
                provider-credentials (into {} (for [[key variable] (get-in compute/registry [:compute (keyword provider) :tofu-env])]
                                               (let [value (get environment (str "COLORS_PAR_" (str/replace (str/upper-case (name key)) "-" "_")))]
                                                 (require-valid (not (missing? value))) [variable value])))
                secrets (merge credentials provider-credentials)
                _ (require-valid (not (journal/bound-secret? documents secrets)))
                directory (Files/createTempDirectory "colors-execution-" (journal/attrs "rwx------"))]
            (try
              (doseq [[filename document] documents] (journal/private-file! directory (key-text filename) (json/generate-string document)))
              (journal/private-file! directory "backend.tf.json" (json/generate-string (:config plan)))
              (let [credential-file (journal/private-file! directory "credentials.tfbackend.json" (json/generate-string credentials))
                    plan-file (journal/private-file! directory "approved.tfplan" "")
                    env (merge (into {} (remove (fn [[key _]] (some #(str/starts-with? key %) ["TF_" "TOFU_" "COLORS_PAR_"])) environment))
                               (into {} (map (fn [[key value]] [(name key) value]) provider-credentials))
                               {"TF_IN_AUTOMATION" "1" "TF_INPUT" "0" "TF_WORKSPACE" "default" "TF_DATA_DIR" (str (.resolve directory ".terraform"))})
                    execute (fn [arguments timeout]
                              (let [argv (into ["tofu"] arguments)]
                                (require-valid (not (journal/bound-secret? argv secrets)))
                                (let [result (runner argv (str directory) env timeout)]
                                  (require-valid (= 0 (:exit result))) (:out result))))]
                (execute ["init" "-input=false" "-no-color" "-reconfigure" (str "-backend-config=" credential-file)] 120000)
                (let [before (execute ["state" "pull"] 120000)
                      current (when (nonblank? before) (state before))]
                  (require-valid (or current (= "absent" (:status presence))))
                  (require-valid (or (nil? current) (empty-state? (:document current)) (= provider (get-in current [:params :provider]))))
                  (if (and (= operation "delete") current (empty-state? (:document current))) {:status "destroyed"}
                      (do
                        (execute (cond-> ["plan" "-input=false" "-no-color" (str "-out=" plan-file)] (= operation "delete") (conj "-destroy")) 1800000)
                        (require-valid (valid-plan? (execute ["show" "-json" plan-file] 120000) operation))
                        (execute ["apply" "-input=false" "-no-color" plan-file] 1800000)
                        (let [after (execute ["state" "pull"] 120000)]
                          (if (and (= operation "delete") (not (nonblank? after))) {:status "destroyed"}
                              (let [{:keys [document params]} (state after)]
                                (if (= operation "delete")
                                  (do (require-valid (empty-state? document)) {:status "destroyed"})
                                  (let [outputs (runtime/flatten-outputs document)]
                                    (require-valid (and (= provider (:provider params)) (not (journal/bound-secret? outputs secrets))))
                                    {:status "ready" :params params :outputs outputs})))))))))
              (finally (journal/cleanup! directory))))))))
(defn converge-state
  ([opts key documents operation presence] (converge-state opts key documents operation presence (into {} (System/getenv)) runtime/run-command))
  ([opts key documents operation presence environment] (converge-state opts key documents operation presence environment runtime/run-command))
  ([opts key documents operation presence environment runner]
   (try (converge* opts key documents operation presence environment runner)
        (catch InterruptedException error (throw error))
        (catch Exception _ {:status "error"}))))
