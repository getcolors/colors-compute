(ns io.github.getcolors.compute-runtime
  "Protected remote-state reads only. No apply, destroy, or state mutation."
  (:require [cheshire.core :as json]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute])
  (:import [java.nio.file Files Path LinkOption]
           [java.nio.file.attribute PosixFilePermissions FileAttribute]
           [java.util.concurrent TimeUnit]))

(defn- private-attributes [mode]
  (into-array FileAttribute [(PosixFilePermissions/asFileAttribute
                             (PosixFilePermissions/fromString mode))]))

(defn- private-file! [directory name contents]
  (let [path (.resolve ^Path directory name)]
    (Files/createFile path (private-attributes "rw-------"))
    (spit (.toFile path) contents)
    (str path)))

(defn- remove-tree! [directory]
  (when directory
    (with-open [paths (Files/walk ^Path directory (make-array java.nio.file.FileVisitOption 0))]
      (doseq [path (reverse (sort-by #(.getNameCount ^Path %) (iterator-seq (.iterator paths))))]
        (Files/deleteIfExists ^Path path)))))

(defn- stop-process! [process descendants]
  (when process
    (with-open [children (.descendants (.toHandle process))]
      (doseq [handle (iterator-seq (.iterator children))] (.destroyForcibly handle)))
    (doseq [handle descendants] (.destroyForcibly handle))
    (.destroyForcibly process)))

(defn- resolve-program [program directory environment]
  (let [absolute-file (fn [path]
                        (let [file (io/file path)]
                          (if (.isAbsolute file) file (io/file directory path))))
        candidates (if (str/includes? program "/")
                     [(absolute-file program)]
                     (when (contains? environment "PATH")
                       (map (fn [entry] (io/file (absolute-file (if (empty? entry) "." entry)) program))
                            (str/split (get environment "PATH") #":" -1))))]
    (or (some #(when (and (.isFile %) (.canExecute %)) (.getAbsolutePath %)) candidates)
        (throw (ex-info "executable unavailable in supplied environment" {})))))

(defn run-command
  "Run fixed argv with an exact environment and a bounded process/output deadline.
  SDK process runners overlay ambient variables, so cannot enforce this boundary."
  [argv dir env timeout-ms]
  (let [active (atom nil)
        descendants (atom #{})]
    (try
      (let [argv (assoc (vec argv) 0 (resolve-program (first argv) dir env))
            builder (ProcessBuilder. ^java.util.List argv)
            _ (.directory builder (io/file dir))
            _ (.clear (.environment builder))
            _ (.putAll (.environment builder) env)
            process (.start builder)
            _ (reset! active process)
            out (future (slurp (.getInputStream process)))
            err (future (slurp (.getErrorStream process)))
            deadline (+ (System/nanoTime) (* 1000000 timeout-ms))
            remaining #(max 0 (quot (- deadline (System/nanoTime)) 1000000))]
        (.close (.getOutputStream process))
        (let [finished? (loop []
                          (with-open [children (.descendants (.toHandle process))]
                            (swap! descendants into (iterator-seq (.iterator children))))
                          (cond
                            (.waitFor process (long (min 25 (remaining))) TimeUnit/MILLISECONDS) true
                            (zero? (remaining)) false
                            :else (recur)))
              output (when finished? (deref out (remaining) ::timeout))
              errors (when finished? (deref err (remaining) ::timeout))]
          (if (and finished? (not= ::timeout output) (not= ::timeout errors))
            {:exit (.exitValue process) :out output :err errors}
            (do
              (stop-process! process @descendants)
              (future-cancel out)
              (future-cancel err)
              {:exit -1 :out "" :err ""}))))
      (catch InterruptedException error
        (stop-process! @active @descendants)
        (throw error))
      (catch Exception _
        (stop-process! @active @descendants)
        {:exit -1 :out "" :err ""}))))

(defn- valid-state? [state]
  (and (map? state)
       (number? (:version state)) (== 4 (:version state))
       (number? (:serial state)) (<= 0 (:serial state) 9007199254740991)
       (== (:serial state) (Math/floor (double (:serial state))))
       (string? (:lineage state)) (not (str/blank? (:lineage state)))
       (map? (:outputs state)) (vector? (:resources state))))

(defn- missing-credential? [value]
  (or (not (string? value)) (str/blank? value)
      (= "REPLACE_ME" (str/upper-case (str/trim value)))))

(defn- bound-secret-in? [params credentials]
  (let [encoded (json/generate-string params)]
    (some (fn [secret]
            (let [escaped (json/generate-string secret)]
              (or (str/includes? encoded secret)
                  (str/includes? encoded (subs escaped 1 (dec (count escaped)))))))
          (vals credentials))))

(defn read-state
  "Read existing remote state through a private OpenTofu backend session.
  Runner receives [argv directory exact-environment timeout-ms]. Params may
  contain sensitive state outputs: callers must not log the returned value."
  ([opts state-key] (read-state opts state-key (into {} (System/getenv)) run-command))
  ([opts state-key environment] (read-state opts state-key environment run-command))
  ([opts state-key environment runner]
   (try
     (let [plan (compute/backend-plan opts state-key)
           credentials (into {} (map (fn [[variable option]]
                                      (let [value (get environment variable)]
                                        (when (missing-credential? value)
                                          (throw (ex-info "invalid backend credential" {})))
                                        [option value])) (:credential_bindings plan)))
           directory (Files/createTempDirectory "colors-compute-" (private-attributes "rwx------"))]
       (try
         (let [_ (private-file! directory "backend.tf.json" (json/generate-string (:config plan)))
               credential-path (private-file! directory "credentials.tfbackend.json" (json/generate-string credentials))
               backend-environment (if (= "r2" (:provider-backend opts))
                                     (dissoc environment "AWS_PROFILE" "AWS_DEFAULT_PROFILE") environment)
               env (merge (into {} (remove (fn [[key _]]
                                             (some #(str/starts-with? key %) ["TF_" "TOFU_" "COLORS_PAR_"])) backend-environment))
                          {"TF_IN_AUTOMATION" "1" "TF_INPUT" "0" "TF_WORKSPACE" "default"
                           "TF_DATA_DIR" (str (.resolve directory ".terraform"))})
               initialized (runner ["tofu" "init" "-input=false" "-no-color" "-reconfigure"
                                    (str "-backend-config=" credential-path)] (str directory) env 120000)]
           (if (not= 0 (:exit initialized))
             {:status "error"}
             (let [pulled (runner ["tofu" "state" "pull"] (str directory) env 120000)]
               (if (not= 0 (:exit pulled))
                 {:status "error"}
                 (let [documents (vec (json/parsed-seq (java.io.StringReader. (:out pulled)) true))
                       state (when (= 1 (count documents)) (first documents))
                       output (get-in state [:outputs :params])
                       params (if (contains? (:outputs state) :params)
                                (when (map? output) (:value output)) {})]
                   (if (and (valid-state? state) (map? params)
                            (not (bound-secret-in? params credentials)))
                     {:status "present" :params params}
                     {:status "error"}))))))
         (finally (remove-tree! directory))))
     (catch InterruptedException error (throw error))
     (catch Exception _ {:status "error"}))))
