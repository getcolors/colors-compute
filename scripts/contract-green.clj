#!/usr/bin/env bb
(require '[babashka.classpath :as cp] '[clojure.java.io :as io])
(let [root (.getParentFile (.getParentFile (.getCanonicalFile (io/file *file*))))]
  (cp/add-classpath (str (io/file root "green/src/clj") ":" (io/file root "green/src/resources"))))
(require '[cheshire.core :as json] '[io.github.getcolors.compute :as compute])
(def operations
  {"validate" compute/validate
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
