(ns io.github.getcolors.compute-runtime
  "Exact-environment process execution and state validation for one compute unit."
  (:require [cheshire.core :as json]
            [io.github.getcolors.compute-local :as local]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute])
  (:import [java.nio.file Files Path LinkOption]
           [java.nio.file.attribute PosixFilePermissions FileAttribute]
           [java.util.concurrent TimeUnit]))

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

(defn valid-state? [state]
  (and (map? state)
       (number? (:version state)) (== 4 (:version state))
       (number? (:serial state)) (<= 0 (:serial state) 9007199254740991)
       (== (:serial state) (Math/floor (double (:serial state))))
       (string? (:lineage state)) (not (str/blank? (:lineage state)))
       (map? (:outputs state)) (vector? (:resources state))))
