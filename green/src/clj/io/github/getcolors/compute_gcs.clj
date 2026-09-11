(ns io.github.getcolors.compute-gcs
  (:require [cheshire.core :as json] [clojure.string :as str])
  (:import [java.net URI URLEncoder] [java.net.http HttpClient HttpRequest HttpRequest$BodyPublishers HttpResponse$BodyHandlers] [java.time Duration]))
(defn encode [value] (str/replace (URLEncoder/encode (str value) "UTF-8") "+" "%20"))
(defn bucket-path [bucket] (str "storage/v1/b/" (encode bucket)))
(defn object-path [bucket key] (str (bucket-path bucket) "/o/" (encode key)))
(defn client [environment runner]
  (let [env (into {} (remove (fn [[k _]] (re-find #"^(COLORS_PAR_|TF_|TOFU_)" k)) environment))
        token (runner ["gcloud" "auth" "print-access-token" "--quiet"] (System/getProperty "user.dir") env 120000)
        _ (when (or (not= 0 (:exit token)) (str/blank? (:out token))) (throw (ex-info "GCS authentication failed" {})))
        http (HttpClient/newHttpClient)]
    (fn [method path body query]
      (let [url (str "https://storage.googleapis.com/" path (when (seq query) (str "?" (str/join "&" (map (fn [[k v]] (str (encode (name k)) "=" (encode v))) query)))))
            builder (-> (HttpRequest/newBuilder (URI/create url)) (.timeout (Duration/ofSeconds 120)) (.header "Authorization" (str "Bearer " (str/trim (:out token)))))
            request (-> builder (.header "Content-Type" "application/json") (.method method (if (nil? body) (HttpRequest$BodyPublishers/noBody) (HttpRequest$BodyPublishers/ofString (json/generate-string body)))) .build)
            response (.send http request (HttpResponse$BodyHandlers/ofString)) code (.statusCode response)]
        (cond (= code 404) nil (= code 412) {:conflict true}
              (<= 200 code 299) (if (str/blank? (.body response)) {} (json/parse-string (.body response) true))
              :else (throw (ex-info (str "GCS operation failed (" code ")") {})))))))
(defn get-object [request bucket key]
  (when-let [metadata (request "GET" (object-path bucket key) nil {})]
    (let [document (request "GET" (object-path bucket key) nil {:alt "media" :generation (:generation metadata)})]
      (when-not document (throw (ex-info "GCS generation disappeared" {})))
      {:document document :etag (:generation metadata)})))
(defn put-object [request bucket key document generation]
  (request "POST" (str "upload/storage/v1/b/" (encode bucket) "/o") document {:uploadType "media" :name key :ifGenerationMatch generation}))
