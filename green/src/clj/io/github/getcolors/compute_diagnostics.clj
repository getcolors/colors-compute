(ns io.github.getcolors.compute-diagnostics
  "Bounded authored errors and command diagnostics; never includes stdout or state."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str])
  (:import [java.net URLEncoder]
           [java.util Base64]))

(def messages
  {"command_failed" "Required command failed."
   "missing_credentials" "Required credentials are not set."
   "state_inconsistent" "Compute state is inconsistent: a resource-free state may only contain the exact requested compute identity for a create retry. Back up the affected state and verify the provider resources before recovering it. Retrying unchanged will fail again. Do not delete state or replace the SSH identity blindly."
   "state_unreadable" "Compute state could not be read."
   "state_absent" "Required compute state is absent."
   "identity_mismatch" "Compute state identity does not match the requested node."
   "unsafe_plan" "Compute plan requires an unauthorized change."
   "invalid_request" "Invalid compute request."
   "key_access_failed" "SSH key access could not be prepared."
   "filesystem_error" "Compute working files could not be accessed."
   "internal_error" "Compute operation failed."})

(def ^:dynamic *context* nil)
(defn stage! [stage]
  (when *context* (swap! *context* assoc :stage stage)))
(defn mutation! []
  (when *context* (swap! *context* assoc :infrastructure_changes "possible")))

(defn- secret-values
  ([value] (secret-values value false))
  ([value inherited?]
   (cond
     (and inherited? (string? value) (not (empty? value))) [value]
     (map? value) (mapcat (fn [[key item]]
                           (secret-values item (or inherited? (boolean (re-find #"(?i)secret|token|password|credential|access.?key|api.?key|private.?key" (name key)))))) value)
     (sequential? value) (mapcat #(secret-values % inherited?) value)
     :else [])))
(defn- variants [secret]
  (let [encoded (json/generate-string secret)]
    (distinct [secret (subs encoded 1 (dec (count encoded))) (URLEncoder/encode secret "UTF-8")
               (str/replace (URLEncoder/encode secret "UTF-8") "+" "%20")
               (.encodeToString (Base64/getEncoder) (.getBytes ^String secret "UTF-8"))])))

(def ^:private structured-pattern
  #"(?:^|\n)[ \t]*[\[{](?!REDACTED\])|\{\s*(?:[\"':}]|[A-Za-z][\w-]*\s*:)|\[\s*(?:[\"'{\[\d-]|true\b|false\b|null\b|\])")

(defn- structured-end [text start]
  (loop [index start stack [] quote-char nil escaped? false]
    (if (>= index (count text)) index
      (let [char (.charAt ^String text index) next-index (inc index)]
        (cond
          quote-char (cond escaped? (recur next-index stack quote-char false)
                           (= char \\) (recur next-index stack quote-char true)
                           (= char quote-char) (recur next-index stack nil false)
                           :else (recur next-index stack quote-char false))
          (contains? #{\" \'} char) (recur next-index stack char false)
          (contains? #{\[ \{} char) (recur next-index (conj stack char) nil false)
          (contains? #{\] \}} char) (cond
                                      (not= (peek stack) (if (= char \]) \[ \{)) (count text)
                                      (= 1 (count stack)) next-index
                                      :else (recur next-index (pop stack) nil false))
          :else (recur next-index stack nil false))))))

(defn- suppress-structured
  [text]
   (let [matcher (re-matcher structured-pattern text)]
     (loop [cursor 0 output ""]
       (if (.find matcher cursor)
         (let [start (loop [index (.start matcher)]
                       (if (contains? #{\[ \{} (.charAt ^String text index)) index (recur (inc index))))
               end (structured-end text start)
               replacement "[structured output suppressed]"]
           (recur end (str output (subs text cursor start) replacement)))
         (str/replace (str output (subs text cursor)) #"\"(?:resources|planned_values|resource_changes|outputs|private_key|private_key_openssh)\"\s*:[\s\S]*" "[structured output suppressed]")))))

(defn redact
  "Redact all known credential forms before limiting diagnostic length."
  [value opts environment]
  (when (and (string? value) (not (str/blank? value)))
    (let [text (-> value
                   (str/replace #"\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)" "")
                   (str/replace #"\u001b\[[0-?]*[ -/]*[@-~]" "")
                   (str/replace #"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]" ""))]
        (let [text (suppress-structured text)
              text (str/replace text #"-----BEGIN [^-\r\n]+-----[\s\S]*?(?:-----END [^-\r\n]+-----|$)" "[private material suppressed]")
              secrets (sort-by count > (distinct (concat (secret-values opts) (secret-values environment))))
              text (reduce (fn [text secret] (reduce #(str/replace %1 %2 "[REDACTED]") text (variants secret))) text secrets)
              text (-> text
                       (str/replace #"(?i)(authorization\s*[:=]\s*)[^\r\n]+" "$1[REDACTED]")
                       (str/replace #"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+" "$1[REDACTED]")
                       (str/replace #"(?i)(\b[A-Za-z0-9_-]*(?:secret|token|password|access[_-]?key|api[_-]?key|private[_-]?key)[A-Za-z0-9_-]*\b[\"']?\s*[:=]\s*)[^\r\n]*" "$1[REDACTED]"))]
          (subs text 0 (min 2000 (count text)))))))

(defn executable
  "Resolve from the command's exact PATH, preserving a shim path for diagnosis."
  [program cwd environment]
  (let [absolute (fn [path] (let [file (io/file path)] (if (.isAbsolute file) file (io/file cwd path))))
        candidates (if (str/includes? program "/") [(absolute program)]
                       (when (string? (get environment "PATH"))
                         (map #(io/file (absolute (if (empty? %) "." %)) program) (str/split (get environment "PATH") #":" -1))))]
    (some #(when (and (.isFile %) (.canExecute %)) (.getAbsolutePath %)) candidates)))
(defn- command-prefix [argv]
  (let [program (.getName (io/file (first argv)))]
    (case program
      "tofu" (vec (take (if (= "state" (second argv)) 3 2) (assoc (vec argv) 0 "tofu")))
      "aws" (vec (take 3 (assoc (vec argv) 0 "aws")))
      "gcloud" (vec (take 3 (assoc (vec argv) 0 "gcloud")))
      "ssh-keygen" ["ssh-keygen"]
      [])))
(defn auth-reason [argv provider result]
  (when (and (= "google" provider) (= ["tofu" "plan"] (vec (take 2 argv)))
             (integer? (:exit result)) (not (zero? (:exit result)))
             (string? (:err result))
             (every? #(str/includes? (:err result) %) ["oauth2:" "invalid_grant" "invalid_rapt"]))
    "google_reauth_required"))

(defn- command-details [argv cwd environment result]
  (let [{:keys [opts source]} (when *context* @*context*)
        path (executable (first argv) cwd environment)
        stderr (redact (:err result) opts source)
        reason (auth-reason argv (:provider-compute opts) result)]
    (cond-> {:command (command-prefix argv)}
      path (assoc :executable (redact path opts source))
      (integer? (:exit result)) (assoc :exit_code (:exit result))
      (contains? #{"executable_not_found" "process_start_failed" "timeout"} (:command_reason result)) (assoc :command_reason (:command_reason result))
      stderr (assoc :stderr stderr)
      reason (assoc :auth_reason reason))))

(defn command-error!
  ([] (command-error! (case (:stage (when *context* @*context*)) "state" "state_unreadable" "access" "key_access_failed" "command_failed")))
  ([code]
   (throw (ex-info "compute command failed"
                   {:compute-code code :diagnostic-stage (:stage (when *context* @*context*))
                    :command-details (:last-command (when *context* @*context*))}))))
(defn wrap-runner [runner]
  (fn [argv cwd environment timeout]
    (try
      (let [result (runner argv cwd environment timeout)]
        (when *context* (swap! *context* assoc :last-command (command-details argv cwd environment result)))
        result)
      (catch InterruptedException error (throw error))
      (catch Exception _
        (when *context* (swap! *context* assoc :last-command (command-details argv cwd environment {})))
        (command-error!)))))

(defn cleanup! [f]
  (let [previous (:stage (when *context* @*context*))]
    (try (stage! "cleanup") (f)
         (catch InterruptedException error (throw error))
         (catch Exception _ (throw (ex-info "compute cleanup failed" {:compute-code "filesystem_error" :diagnostic-stage "cleanup"})))
         (finally (when previous (stage! previous))))))

(defn- code-for [error stage]
  (or (:compute-code (ex-data error))
      (let [message (or (.getMessage ^Exception error) "")]
        (cond
          (re-find #"identity|provider mismatch|state holds|provider cannot change|backend changed" message) "identity_mismatch"
          (re-find #"absent|state is required|state required|required state|does not exist" message) "state_absent"
          (re-find #"credential" message) "missing_credentials"
          (= stage "state") "state_unreadable"
          (= stage "plan-validation") "unsafe_plan"
          (or (instance? java.io.IOException error)
              (re-find #"unsafe.*(?:file|directory)|invalid local (?:directory|file|state)|compute working" message)) "filesystem_error"
          (= stage "credentials") "missing_credentials"
          (= stage "access") "key_access_failed"
          (= stage "cleanup") "filesystem_error"
          (contains? #{"validate" "build"} stage) "invalid_request"
          :else "internal_error"))))
(defn failure [error]
  (let [context (when *context* @*context*)
        stage (or (:diagnostic-stage (ex-data error)) (:stage context) "validate")
        code (code-for error stage)
        credential (when (= code "missing_credentials")
                     (second (re-matches #"required credential is not set: (COLORS_PAR_[A-Z0-9_]+)" (or (.getMessage ^Exception error) ""))))]
    {:status "error"
     :error (merge (cond-> {:code code :stage stage :message (get messages code (messages "internal_error"))
                            :infrastructure_changes (get context :infrastructure_changes "none")}
                    credential (assoc :credential credential))
                   (:command-details (ex-data error)))}))
