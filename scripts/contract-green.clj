#!/usr/bin/env bb
(require '[babashka.classpath :as cp] '[clojure.java.io :as io])
(let [root (.getParentFile (.getParentFile (.getCanonicalFile (io/file *file*))))]
  (cp/add-classpath (str (io/file root "green/src/clj") ":" (io/file root "green/src/resources"))))
(require '[cheshire.core :as json] '[io.github.getcolors.compute :as compute]
         '[io.github.getcolors.compute-runtime :as runtime]
         '[io.github.getcolors.compute-request :as request]
         '[io.github.getcolors.compute-deployment-request :as deployment]
         '[io.github.getcolors.compute-planning :as planning]
         '[io.github.getcolors.compute-lifecycle :as lifecycle]
         '[io.github.getcolors.compute-coordination :as coordination]
         '[io.github.getcolors.compute-journal :as journal])
(defn read-state-case [opts key environment responses]
  (let [responses (atom responses)]
    (runtime/read-state opts key (into {} (map (fn [[k v]] [(name k) v]) environment))
      (fn [_ _ _ _]
        (let [response (first @responses)]
          (swap! responses next)
          response)))))
(defn journal-case
  ([opts environment response body] (journal-case opts environment response body nil))
  ([opts environment response body intent]
   (let [env (into {} (map (fn [[k v]] [(name k) v]) environment))
         runner (fn [argv _ _ _]
                  (when (= "get-object" (nth argv 2))
                    (if (map? body)
                      (with-open [stream (io/output-stream (nth argv 7))]
                        (.write stream (.decode (java.util.Base64/getDecoder) ^String (:bytes_base64 body))))
                      (spit (nth argv 7) body)))
                  response)]
     (if (nil? intent) (journal/journal-get opts env runner)
         (journal/journal-put opts intent env runner)))))
(def operations
  {"deployment_requests" deployment/deployment-requests
   "plan_deployment" planning/plan-deployment
   "lifecycle" lifecycle/lifecycle
   "provider_request" request/provider-request
   "journal_case" journal-case
   "coordination" coordination/coordination
   "read_state_case" read-state-case
   "validate" compute/validate
   "credential_requirements" compute/credential-requirements
   "state_keys" compute/state-keys
   "expand" compute/expand
   "collect" compute/collect
   "state_decision" compute/state-decision
   "render_template" compute/render-template
   "backend_plan" compute/backend-plan
   "provider_plan" compute/provider-plan})
(doseq [line (line-seq (java.io.BufferedReader. *in*))]
  (println (json/generate-string
            (try
              (let [{:keys [op args]} (json/parse-string line true)]
                (if-let [operation (get operations op)]
                  (apply operation args)
                  (throw (ex-info (str "unknown operation: " op) {}))))
              (catch Exception error {:error (.getMessage error)})))))
