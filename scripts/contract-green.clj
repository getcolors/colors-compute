#!/usr/bin/env bb
(require '[babashka.classpath :as cp] '[babashka.deps :as deps] '[clojure.java.io :as io])
(let [root (.getParentFile (.getParentFile (.getCanonicalFile (io/file *file*))))]
  (deps/add-deps {:deps {'io.github.getcolors/colors-compute {:local/root (str (io/file root "green"))}}})
  (cp/add-classpath (str (io/file root "green/src/clj") ":" (io/file root "green/src/resources"))))
(require '[cheshire.core :as json] '[io.github.getcolors.compute :as compute]
         '[io.github.getcolors.compute-request :as request]
         '[io.github.getcolors.compute-node :as node]
         '[io.github.getcolors.compute-diagnostics :as diagnostic])
(def operations
  {"sanitize_error" diagnostic/redact
   "node_runtime_error" (fn [opts request operation] (node/compute-node! opts request operation {} {}))
   "node_plan_valid" (fn [opts request] (try (node/node-plan opts request) true (catch Exception _ false)))
   "node_plan" node/node-plan
   "provider_request" request/provider-request
   "validate" compute/validate
   "credential_requirements" compute/credential-requirements
   "render_template" compute/render-template
   "backend_plan" compute/backend-plan
   "provider_plan" compute/provider-plan})
(doseq [line (line-seq (java.io.BufferedReader. *in*))]
  (println (json/generate-string
            (try
              (let [{:keys [op args]} (json/parse-string line true)]
                (if-let [operation (get operations op)] (apply operation args)
                    (throw (ex-info (str "unknown operation: " op) {}))))
              (catch Exception error {:error (.getMessage error)})))))
