(ns io.github.getcolors.compute-journal
  "AWS CLI conditional journal object transport. No retries or provider dispatch."
  (:require [cheshire.core :as json]
            [io.github.getcolors.compute-gcs :as gcs]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-coordination :as coordination]
            [io.github.getcolors.compute-lifecycle :as lifecycle]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files Path]
           [java.nio.file.attribute PosixFilePermissions FileAttribute]
           [java.nio.charset StandardCharsets]
           [java.nio ByteBuffer]))

(def ^:private limit-bytes (* 2 1024 1024))
(defn- nonblank? [value] (and (string? value) (not (str/blank? value))))
(defn- exact? [value fields] (and (map? value) (= (set (keys value)) fields)))
(defn ^:no-doc attrs [mode] (into-array FileAttribute [(PosixFilePermissions/asFileAttribute (PosixFilePermissions/fromString mode))]))
(defn ^:no-doc private-file! [directory name content]
  (let [path (.resolve ^Path directory name)]
    (Files/createFile path (attrs "rw-------"))
    (spit (.toFile path) content)
    (str path)))
(defn ^:no-doc cleanup! [directory]
  (with-open [paths (Files/walk ^Path directory (make-array java.nio.file.FileVisitOption 0))]
    (doseq [path (reverse (sort-by #(.getNameCount ^Path %) (iterator-seq (.iterator paths))))]
      (Files/deleteIfExists ^Path path))))
(defn ^:no-doc parse-one [text]
  (let [documents (vec (json/parsed-seq (java.io.StringReader. text) true))]
    (when-not (= 1 (count documents)) (throw (ex-info "invalid JSON" {})))
    (first documents)))
(defn- serialized [value]
  (let [text (json/generate-string value)]
    (when (> (alength (.getBytes text StandardCharsets/UTF_8)) limit-bytes)
      (throw (ex-info "journal too large" {})))
    text))
(defn- settings [opts]
  (compute/state-keys (:profile opts) [])
  (let [key (str (:profile opts) "/compute/coordination.json")
        plan (compute/backend-plan opts key)
        backend (compute/backend-settings opts key)]
    (when-not (and (nonblank? (:bucket backend)) (nonblank? (:region backend)))
      (throw (ex-info "invalid backend settings" {})))
    {:kind (:provider-backend opts) :bucket (:bucket backend) :region (:region backend)
     :endpoint (get-in backend [:endpoints :s3]) :key key :bindings (:credential_bindings plan)}))
(defn- credentials [settings environment]
  (into {} (map (fn [[variable option]]
                  (let [value (get environment variable)]
                    (when-not (and (nonblank? value)
                                   (not= "REPLACE_ME" (str/upper-case (str/trim value)))
                                   (not (re-find #"[\r\n]" value)))
                      (throw (ex-info "invalid backend credential" {})))
                    [option value])) (:bindings settings))))
(defn- child-environment! [settings credentials directory environment]
  (let [r2? (= "r2" (:kind settings))
        cleaned (into {} (remove (fn [[key _]]
                                   (or (str/starts-with? key "COLORS_PAR_")
                                       (and r2? (str/starts-with? key "AWS_") (not= "AWS_CA_BUNDLE" key)))) environment))]
    (merge cleaned {"AWS_PAGER" "" "AWS_CLI_AUTO_PROMPT" "off" "AWS_MAX_ATTEMPTS" "1"}
           (when r2?
             {"AWS_SHARED_CREDENTIALS_FILE"
              (private-file! directory "credentials" (str "[default]\naws_access_key_id = " (get credentials "access_key")
                                                          "\naws_secret_access_key = " (get credentials "secret_key") "\n"))
              "AWS_CONFIG_FILE" (private-file! directory "config" "")
              "AWS_REQUEST_CHECKSUM_CALCULATION" "when_required"
              "AWS_RESPONSE_CHECKSUM_VALIDATION" "when_required"}))))
(defn ^:no-doc service-code [result operation]
  (when (and (number? (:exit result)) (not (zero? (:exit result))) (string? (:err result)))
    (second (re-find (re-pattern (str "^\\s*(?:aws: \\[ERROR\\]: )?An error occurred \\(([A-Za-z0-9]+)\\) when calling the " operation " operation(?: \\(reached max retries: [0-9]+\\))?:"))
                     (:err result)))))
(defn ^:no-doc bound-secret? [value credentials]
  (let [text (json/generate-string value)]
    (boolean
     (some (fn [secret]
             (let [encoded (json/generate-string secret)]
               (or (str/includes? text secret) (str/includes? text (subs encoded 1 (dec (count encoded)))))))
           (vals credentials)))))
(defn- safe-result [result credentials]
  (if (bound-secret? result credentials) {:status "error"} result))
(defn- read-body [body]
  (with-open [stream (io/input-stream body)]
    (let [bytes (.readNBytes stream (inc limit-bytes))]
      (when (> (alength bytes) limit-bytes) (throw (ex-info "journal too large" {})))
      (let [document (parse-one (str (.decode (.newDecoder StandardCharsets/UTF_8) (ByteBuffer/wrap bytes))))]
        (when-not (map? document) (throw (ex-info "invalid document" {})))
        document))))
(defn- s3-call-session [opts environment runner intent]
  (let [settings (settings opts)
        credentials (credentials settings environment)
        write? (some? intent)
        _ (when write?
            (let [condition (:condition intent)
                  expected {:profile (:profile opts) :provider (:provider-compute opts)
                            :backend (cond-> (select-keys settings [:kind :bucket :region])
                                       (= "r2" (:kind settings)) (assoc :endpoint (:endpoint settings)))}]
              (when-not (and (exact? intent #{:condition :document})
                             (or (coordination/valid-document? (:document intent)) (lifecycle/valid-document? (:document intent)))
                             (= expected (get-in intent [:document :identity]))
                             (or (and (exact? condition #{:if_none_match}) (= "*" (:if_none_match condition)))
                                 (and (exact? condition #{:if_match}) (nonblank? (:if_match condition)))))
                (throw (ex-info "invalid journal intention" {})))))
        _ (when (and write? (bound-secret? intent credentials))
            (throw (ex-info "backend credential in journal intention" {})))
        content (if write? (serialized (:document intent)) "")
        directory (Files/createTempDirectory "colors-journal-" (attrs "rwx------"))]
    (try
      (let [body (private-file! directory "document.json" content)
            child-env (child-environment! settings credentials directory environment)
            command (into ["aws" "s3api" (if write? "put-object" "get-object")
                           "--bucket" (:bucket settings) "--key" (:key settings)]
                          (concat (if write?
                                    ["--body" body "--content-type" "application/json"
                                     (if (contains? (:condition intent) :if_match) "--if-match" "--if-none-match")
                                     (or (get-in intent [:condition :if_match]) "*")]
                                    [body])
                                  ["--region" (:region settings) "--output" "json" "--no-cli-pager"]
                                  (when (= "r2" (:kind settings)) ["--endpoint-url" (:endpoint settings)])))
            _ (when (bound-secret? command credentials)
                (throw (ex-info "backend credential in command" {})))
            result (runner command (str directory) child-env 120000)]
        (safe-result
         (if (= 0 (:exit result))
           (let [metadata (parse-one (:out result)) etag (:ETag metadata)]
             (if-not (nonblank? etag) {:status "error"}
               (if write? {:status "written" :etag etag}
                 {:status "present" :etag etag :document (read-body body)})))
           (let [code (service-code result (if write? "PutObject" "GetObject"))]
             (cond (and (not write?) (= "NoSuchKey" code)) {:status "absent"}
                   (and write? (contains? #{"PreconditionFailed" "ConditionalRequestConflict"} code)) {:status "conflict"}
                   :else {:status "error"}))) credentials))
      (finally (cleanup! directory)))))

(defn- call-session [opts environment runner intent]
  (if (not= "gcs" (:provider-backend opts)) (s3-call-session opts environment runner intent)
    (let [settings (settings opts) bucket (:bucket settings) key (:key settings)
          expected {:profile (:profile opts) :provider (:provider-compute opts) :backend (select-keys settings [:kind :bucket :region])}]
      (if (nil? intent)
        (if-let [result (gcs/get-object (gcs/client environment runner) bucket key)] (assoc result :status "present") {:status "absent"})
        (let [condition (:condition intent) generation (or (:if_match condition) "0")]
          (when-not (and (exact? intent #{:condition :document})
                         (or (coordination/valid-document? (:document intent)) (lifecycle/valid-document? (:document intent)))
                         (= expected (get-in intent [:document :identity]))
                         (or (and (exact? condition #{:if_none_match}) (= "*" (:if_none_match condition)))
                             (and (exact? condition #{:if_match}) (string? (:if_match condition)) (re-matches #"[0-9]+" (:if_match condition)))))
            (throw (ex-info "invalid journal intention" {})))
          (serialized (:document intent))
          (let [result (gcs/put-object (gcs/client environment runner) bucket key (:document intent) generation)]
            (cond (:conflict result) {:status "conflict"} (:generation result) {:status "written" :etag (:generation result)} :else {:status "error"})))))))

(defn journal-get
  "Read the derived coordination object; present content remains untrusted."
  ([opts] (journal-get opts (into {} (System/getenv)) runtime/run-command))
  ([opts environment] (journal-get opts environment runtime/run-command))
  ([opts environment runner]
   (try (call-session opts environment runner nil)
        (catch InterruptedException error (throw error))
        (catch Exception _ {:status "error"}))))

(defn journal-put
  "Perform one validated conditional write, without retry or provider dispatch."
  ([opts intent] (journal-put opts intent (into {} (System/getenv)) runtime/run-command))
  ([opts intent environment] (journal-put opts intent environment runtime/run-command))
  ([opts intent environment runner]
   (try
     (when (nil? intent) (throw (ex-info "invalid journal intention" {})))
     (call-session opts environment runner intent)
     (catch InterruptedException error (throw error))
     (catch Exception _ {:status "error"}))))

(defn ^:no-doc backend-session [opts environment callback]
  (let [settings (settings opts) credentials (credentials settings environment)
        directory (Files/createTempDirectory "colors-object-" (attrs "rwx------"))]
    (try (callback directory (child-environment! settings credentials directory environment) credentials settings)
         (finally (cleanup! directory)))))
