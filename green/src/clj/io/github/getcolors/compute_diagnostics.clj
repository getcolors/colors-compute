(ns io.github.getcolors.compute-diagnostics
  "Safe lifecycle diagnostics; never format exceptions, credentials or state."
  (:require [clojure.java.io :as io] [clojure.string :as str]
            [io.github.getcolors.compute-ssh :as ssh]))
(def messages
  {"missing-tool" ["Required infrastructure tools are missing from PATH." "Install the listed tools or load the deployment environment, then retry."]
   "state-unreadable" ["Cannot read infrastructure state; absence has not been established." "Check backend access and credentials before retrying. Do not remove state or ownership records."]
   "failed-operation-without-state" ["A previous infrastructure operation failed and its state file is missing. Cloud resources may still exist." "Inspect provider resources and reconcile the failed operation using the reviewed recovery procedure before retrying."]
   "recorded-state-missing" ["The ownership journal records infrastructure whose state file is missing." "Inspect provider resources and recover the recorded state before retrying. Do not reset the journal."]})
(defn failure
  ([code] (failure code []))
  ([code tools]
   (let [[message hint] (get messages code)
         diagnostic (cond-> {:code code :message message :hint hint}
                      (= code "missing-tool") (assoc :tools (vec (sort (filter #{"tofu" "aws" "gcloud" "oci" "ssh-keygen"} (set tools))))))]
     (ex-info code {::diagnostic diagnostic}))))
(defn result [error]
  (when-let [d (::diagnostic (ex-data error))]
    {:status "error" :diagnostics [d]
     :errors [(str (:message d) (when (seq (:tools d)) (str " Missing: " (str/join ", " (:tools d)) ".")) " Next: " (:hint d))]}))
(defn required-tools [opts]
  (vec (sort (set (cond-> ["tofu"]
                   (contains? #{"s3" "r2"} (get opts :provider-backend "r2")) (conj "aws")
                   (= "gcs" (:provider-backend opts)) (conj "gcloud")
                   (or (= "oci" (:provider-backend opts)) (= "oci" (:provider-compute opts))) (conj "oci")
                   (= "managed" (:mode (ssh/mode opts))) (conj "ssh-keygen"))))))
(defn missing-tools [opts environment]
  (let [paths (when (contains? environment "PATH") (str/split (get environment "PATH") #":" -1))]
    (vec (remove (fn [tool]
                   (some (fn [entry] (let [file (io/file (if (empty? entry) "." entry) tool)]
                                      (and (.isFile file) (.canExecute file)))) paths))
                 (required-tools opts)))))
