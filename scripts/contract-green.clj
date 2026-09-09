#!/usr/bin/env bb
(require '[babashka.classpath :as cp] '[clojure.java.io :as io])
(let [root (.getParentFile (.getParentFile (.getCanonicalFile (io/file *file*))))]
  (cp/add-classpath (str (io/file root "green/src/clj") ":" (io/file root "green/src/resources"))))
(require '[cheshire.core :as json] '[io.github.getcolors.compute :as compute]
         '[io.github.getcolors.compute-runtime :as runtime]
         '[io.github.getcolors.compute-coordination :as coordination])
(defn read-state-case [opts key environment responses]
  (let [responses (atom responses)]
    (runtime/read-state opts key (into {} (map (fn [[k v]] [(name k) v]) environment))
      (fn [_ _ _ _]
        (let [response (first @responses)]
          (swap! responses next)
          response)))))
(def operations
  {"coordination" coordination/coordination
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
