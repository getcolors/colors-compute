(ns io.github.getcolors.compute-oci
  (:require [cheshire.core :as json] [clojure.string :as str])
  (:import [java.net URLEncoder]))
(defn encode [value] (str/replace (URLEncoder/encode (str value) "UTF-8") "+" "%20"))
(defn bucket-path [opts] (str "/n/" (encode (:oci-namespace opts)) "/b/" (encode (:oci-bucket opts))))
(defn object-path [opts key] (str (bucket-path opts) "/o/" (encode key)))
(defn client [opts environment runner]
  (let [env (into {} (remove (fn [[k _]] (re-find #"^(COLORS_PAR_|TF_|TOFU_)" k)) environment))]
    (fn [method path body query headers]
      (let [url (str "https://objectstorage." (:oci-region opts) ".oraclecloud.com" path
                     (when (seq query) (str "?" (str/join "&" (map (fn [[k v]] (str (encode (name k)) "=" (encode v))) query)))))
            args (cond-> ["oci" "raw-request" "--no-retry" "--auth" (if (= "SecurityToken" (get opts :oci-auth "SecurityToken")) "security_token" "api_key")
                          "--profile" (get opts :oci-config-file-profile "DEFAULT") "--region" (:oci-region opts)
                          "--http-method" method "--target-uri" url "--request-headers" (json/generate-string headers)]
                   (some? body) (into ["--request-body" (json/generate-string body)]))
            result (runner args (System/getProperty "user.dir") env 120000)
            _ (when-not (= 0 (:exit result)) (throw (ex-info "OCI request failed" {})))
            response (json/parse-string (:out result) true) code (parse-long (first (str/split (:status response) #" ")))]
        (cond (= 404 code) nil (contains? #{409 412} code) {:conflict true}
              (<= 200 code 299) {:data (:data response) :headers (into {} (map (fn [[k v]] [(keyword (str/lower-case (name k))) v]) (:headers response)))}
              :else (throw (ex-info (str "OCI request failed (" code ")") {})))))))
