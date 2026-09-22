(ns io.github.getcolors.compute-ssh
  "Named encrypted SSH resources and caller-scoped dedicated agents."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [clojure.walk :as walk])
  (:import [java.net URI]
           [java.nio.file Files]
           [java.nio.file.attribute PosixFilePermissions FileAttribute]
           [java.util.concurrent TimeUnit]))

(defn- need [ok message] (when-not ok (throw (ex-info message {}))))
(defn- matches [pattern value] (and (string? value) (boolean (re-matches pattern value))))
(defn- canonical [value]
  (json/generate-string (walk/postwalk #(if (map? %) (into (sorted-map) %) %) value)))

(defn ssh-plan [opts request]
  (need (and (map? request) (every? #{:name :workdir :passphrase_env :backend :expected :new_passphrase_env :allow_delete :consumers_destroyed :lock_token} (keys request))) "invalid SSH request")
  (let [{:keys [name workdir passphrase_env]} request
        profile (:profile opts)
        _ (need (and (matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" profile)
                     (matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" name)
                     (string? workdir) (.isAbsolute (io/file workdir))
                     (= workdir (str (.normalize (.toPath (io/file workdir)))))
                     (not (re-find #"[\\\x00]" workdir))) "invalid SSH resource identity")
        _ (need (matches #"COLORS_PAR_[A-Z][A-Z0-9_]*" passphrase_env) "invalid passphrase binding")
        override (get request :backend {})
        _ (need (and (map? override) (every? #{:provider-backend :s3-prefix :s3-bucket :s3-region :r2-bucket :r2-endpoint :oci-bucket :oci-region :oci-namespace :gcs-bucket} (keys override))) "invalid SSH backend override")
        settings (merge opts override)
        kind (:provider-backend settings)
        _ (need (contains? #{"local" "s3" "r2" "oci" "gcs"} kind) "invalid SSH backend")
        prefix (get settings :s3-prefix "")
        _ (need (and (string? prefix) (or (empty? prefix) (every? #(matches #"[A-Za-z0-9][A-Za-z0-9_.-]*" %) (str/split prefix #"/" -1)))) "invalid SSH prefix")
        directory (str (io/file workdir profile "ssh" name))
        key (str/join "/" (remove empty? [prefix profile "ssh" name "resource.json"]))
        storage (if (= kind "local")
                  {:kind kind :path (str directory "/resource.json")}
                  (let [bucket (get settings (keyword (str kind "-bucket")))
                        _ (need (matches #"[A-Za-z0-9][A-Za-z0-9._-]{1,221}" bucket) "invalid SSH bucket")
                        region (if (= kind "r2") "auto" (get settings (keyword (str kind "-region"))))
                        _ (when-not (= kind "gcs") (need (matches #"[A-Za-z0-9_-]+" region) "invalid SSH region"))
                        _ (when (= kind "oci") (need (matches #"[A-Za-z0-9][A-Za-z0-9_-]*" (:oci-namespace settings)) "invalid OCI namespace"))
                        endpoint (case kind
                                   "r2" (:r2-endpoint settings)
                                   "oci" (str "https://" (:oci-namespace settings) ".compat.objectstorage." region ".oraclecloud.com")
                                   nil)
                        _ (when (contains? #{"r2" "oci"} kind)
                            (need (matches #"https://[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?::[0-9]{1,5})?/?" endpoint) "invalid SSH endpoint")
                            (let [uri (URI. endpoint)]
                              (need (and (seq (.getHost uri)) (every? #(matches #"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?" %) (str/split (.getHost uri) #"\." -1)) (or (= -1 (.getPort uri)) (<= 1 (.getPort uri) 65535))) "invalid SSH endpoint")))]
                    (cond-> {:kind kind :bucket bucket}
                      (not= kind "gcs") (assoc :region region)
                      endpoint (assoc :endpoint (str/replace endpoint #"/$" "") :credential_prefix (str/upper-case kind)))))
        identity (cond-> {:version 2 :profile profile :name name :storage storage}
                   (not= kind "local") (assoc :object_key key))]
    {:status "planned" :directory directory :object_key key :storage storage :reference (canonical identity)}))

(def ^:private base-env
  #{"PATH" "HOME" "TMPDIR" "SYSTEMROOT" "AWS_ACCESS_KEY_ID" "AWS_SECRET_ACCESS_KEY" "AWS_SESSION_TOKEN"
    "AWS_PROFILE" "AWS_DEFAULT_PROFILE" "AWS_CONFIG_FILE" "AWS_SHARED_CREDENTIALS_FILE" "AWS_CA_BUNDLE"
    "GOOGLE_APPLICATION_CREDENTIALS" "CLOUDSDK_CONFIG" "COLORS_PAR_R2_ACCESS_KEY_ID"
    "COLORS_PAR_R2_SECRET_ACCESS_KEY" "COLORS_PAR_OCI_ACCESS_KEY_ID" "COLORS_PAR_OCI_SECRET_ACCESS_KEY"})
(defn- environment [source requests]
  (select-keys source (into base-env (mapcat #(keep % [:passphrase_env :new_passphrase_env]) requests))))
(defn- executable [program env]
  (or (some (fn [dir] (let [file (io/file dir program)] (when (.canExecute file) (.getAbsolutePath file))))
            (str/split (get env "PATH" "") #":"))
      (throw (ex-info "SSH adapter requires python3" {}))))

(defn- process-closer [process writer output errors cleanup]
  ;; The cleanup worker is started exactly once. Interrupted callers drain it
  ;; before restoring their interruption flag, including failed startup paths.
  (let [closing (delay
                  (future
                    (try
                      (.close (.getOutputStream process))
                      (when-not (.waitFor process 10 TimeUnit/SECONDS)
                        (.destroy process)
                        (when-not (.waitFor process 5 TimeUnit/SECONDS)
                          (.destroyForcibly process)
                          (need (.waitFor process 5 TimeUnit/SECONDS)
                                "SSH adapter did not terminate")))
                      (finally
                        (future-cancel output) (future-cancel errors)
                        (cleanup)))))]
    (fn []
      (let [interrupted (atom (Thread/interrupted))]
        (try
          (loop []
            (let [result (try {:value @@closing}
                              (catch InterruptedException _
                                (reset! interrupted true)
                                {:retry true}))]
              (if (:retry result) (recur) (:value result))))
          (finally
            (when @interrupted (.interrupt (Thread/currentThread)))))))))

(defn- start! [message env]
  (let [python (executable "python3" env)
        directory (.toFile (Files/createTempDirectory "colors-ssh-runtime-"
                    (into-array FileAttribute [(PosixFilePermissions/asFileAttribute (PosixFilePermissions/fromString "rwx------"))])))
        script (io/file directory "ssh-adapter")
        _ (spit script (slurp (io/resource "colors_compute/ssh_adapter.py")))
        _ (Files/setPosixFilePermissions (.toPath script) (PosixFilePermissions/fromString "rwx------"))
        builder (ProcessBuilder. ^java.util.List [python (.getAbsolutePath script)])
        _ (.clear (.environment builder))
        _ (.putAll (.environment builder) env)
        process (try (.start builder) (catch Exception error (.delete script) (.delete directory) (throw error)))
        writer (io/writer (.getOutputStream process))
        output (future (.readLine (java.io.BufferedReader. (io/reader (.getInputStream process)))))
        errors (future (slurp (.getErrorStream process)))
        close! (process-closer process writer output errors
                               #(do (.delete script) (.delete directory)))]
    (try
      (.write writer (str (json/generate-string message) "\n")) (.flush writer)
      {:output output :close close!}
      (catch Throwable error (close!) (throw error)))))
(defn- ready! [handle]
  (let [line (deref (:output handle) 180000 ::timeout)]
    (need (and (string? line) (not (str/blank? line))) "SSH resource operation failed or timed out")
    (json/parse-string line true)))

(defn ssh-resource!
  ([opts request] (ssh-resource! opts request "create" (System/getenv)))
  ([opts request operation environment-map]
   (let [plan (ssh-plan opts request)]
     (if (= operation "build") (assoc plan :status "built")
       (let [handle (start! {:plan plan :request request :operation operation}
                            (environment environment-map [request]))]
         (try (ready! handle) (finally ((:close handle)))))))))

(defn start-agent!
  ([resources environment-map register!] (start-agent! resources environment-map register! 900))
  ([resources environment-map register! lifetime]
   (let [entries (mapv #(assoc (select-keys % [:request :resource]) :plan (ssh-plan (:opts %) (:request %))) resources)
         handle (start! {:operation "agent" :resources entries :lifetime lifetime}
                        (environment environment-map (map :request entries)))]
     (try
       (register! :resource (:close handle))
       (let [result (ready! handle)]
         (need (= "ready" (:status result)) "SSH agent setup failed")
         ;; References are opaque strings, not keyword names.
         (update result :identities #(into {} (map (fn [[k v]] [(if (keyword? k) (subs (str k) 1) k) v]) %))))
       (catch Throwable error ((:close handle)) (throw error))))))
