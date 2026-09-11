(ns io.github.getcolors.compute-oci
  (:require [cheshire.core :as json] [clojure.string :as str])
  (:import [java.net URLEncoder]))
(defn encode [value] (str/replace (URLEncoder/encode (str value) "UTF-8") "+" "%20"))
(defn bucket-path [opts] (str "/n/" (encode (:oci-namespace opts)) "/b/" (encode (:oci-bucket opts))))
(defn object-path [opts key] (str (bucket-path opts) "/o/" (encode key)))
(defn client
  ([opts environment runner] (client opts environment runner "objectstorage"))
  ([opts environment runner service]
  (when-not (contains? #{"objectstorage" "iaas"} service) (throw (ex-info "invalid OCI service" {})))
  (let [env (into {} (remove (fn [[k _]] (re-find #"^(COLORS_PAR_|TF_|TOFU_)" k)) environment))]
    (fn [method path body query headers]
      (let [url (str "https://" service "." (:oci-region opts) ".oraclecloud.com" path
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
              :else (throw (ex-info (str "OCI request failed (" code ")") {}))))))))

(defn availability-domains [opts]
  (if-not (contains? opts :oci-availability-domains) [(:oci-availability-domain opts)]
    (let [domains (:oci-availability-domains opts)]
      (when-not (and (vector? domains) (<= 1 (count domains) 16) (= (count domains) (count (set domains)))
                     (every? #(and (string? %) (re-matches #"[A-Za-z0-9][A-Za-z0-9:_-]{0,255}" %)) domains))
        (throw (ex-info "invalid OCI availability domains" {})))
      domains)))
(declare validate-cpus!)
(defn place-node [opts stage node-id]
  (when (and (= "oci" (:provider-compute opts)) (= "node" stage)) (validate-cpus! opts))
  (if-not (and (= "oci" (:provider-compute opts)) (contains? opts :oci-availability-domains)) opts
    (let [domains (availability-domains opts)]
      (if (not= stage "node") opts
        (let [index (when (string? node-id) (second (re-find #"(?:^|-)(0|[1-9][0-9]{0,2})$" node-id)))]
          (when-not index (throw (ex-info "OCI placement requires an indexed node ID" {})))
          (assoc opts :oci-availability-domain (nth domains (mod (parse-long index) (count domains)))))))))

(defn validate-cpus! [opts]
  (let [ocpus (:oci-ocpus opts) vcpus (:oci-vcpus opts) values (filter some? [ocpus vcpus])]
    (when-not (= 1 (count values)) (throw (ex-info "OCI requires exactly one of oci-ocpus or oci-vcpus" {})))
    (when-not (and (number? (first values)) (Double/isFinite (double (first values))) (pos? (first values))
                   (or (nil? vcpus) (== (double vcpus) (Math/floor (double vcpus)))))
      (throw (ex-info "invalid OCI CPU count" {})))))
