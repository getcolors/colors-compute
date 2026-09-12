(ns io.github.getcolors.compute-gcs
  (:require [cheshire.core :as json] [clojure.string :as str])
  (:import [java.net URI URLEncoder] [java.net.http HttpClient HttpRequest HttpRequest$BodyPublishers HttpResponse$BodyHandlers] [java.time Duration]))
(defn encode [value] (str/replace (URLEncoder/encode (str value) "UTF-8") "+" "%20"))
(defn bucket-path [bucket] (str "storage/v1/b/" (encode bucket)))
(defn object-path [bucket key] (str (bucket-path bucket) "/o/" (encode key)))
(defn send-request [http request]
  (let [response (.send ^HttpClient http request (HttpResponse$BodyHandlers/ofString))]
    {:code (.statusCode response) :body (.body response)}))
(defn client [environment runner]
  (let [env (into {} (remove (fn [[k _]] (re-find #"^(COLORS_PAR_|TF_|TOFU_)" k)) environment))
        token (runner ["gcloud" "auth" "print-access-token" "--quiet"] (System/getProperty "user.dir") env 120000)
        _ (when (or (not= 0 (:exit token)) (str/blank? (:out token))) (throw (ex-info "GCS authentication failed" {})))
        http (HttpClient/newHttpClient)]
    (fn request [method path body query]
      (let [url (str (if (str/starts-with? path "v1/projects/") "https://cloudresourcemanager.googleapis.com/" "https://storage.googleapis.com/") path (when (seq query) (str "?" (str/join "&" (map (fn [[k v]] (str (encode (name k)) "=" (encode v))) query)))))
            builder (-> (HttpRequest/newBuilder (URI/create url)) (.timeout (Duration/ofSeconds 120)) (.header "Authorization" (str "Bearer " (str/trim (:out token)))))
            http-request (-> builder (.header "Content-Type" "application/json") (.method method (if (nil? body) (HttpRequest$BodyPublishers/noBody) (HttpRequest$BodyPublishers/ofString (json/generate-string body)))) .build)
            {:keys [code body]} (send-request http http-request)]
        (cond (= code 404) (do
                            (when (and (= method "GET") (str/starts-with? path "storage/v1/b/") (str/includes? path "/o/"))
                              (let [parent (first (str/split path #"/o/" 2)) bucket (request "GET" parent nil {})]
                                (when (nil? bucket) (throw (ex-info "GCS bucket missing" {})))
                                (when-not (and (string? (:name bucket)) (= parent (bucket-path (:name bucket))))
                                  (throw (ex-info "invalid GCS bucket metadata" {})))))
                            nil) (= code 412) {:conflict true}
              (<= 200 code 299) (if (str/blank? body) {} (json/parse-string body true))
              :else (throw (ex-info (str "GCS operation failed (" code ")") {})))))))
(defn get-object
  ([request bucket key] (get-object request bucket key nil))
  ([request bucket key max-bytes]
   (when-let [metadata (request "GET" (object-path bucket key) nil {})]
    (when (and max-bytes (or (not (and (string? (:size metadata)) (re-matches #"[0-9]+" (:size metadata))))
                             (> (bigint (:size metadata)) max-bytes)))
      (throw (ex-info "GCS object too large or missing size" {})))
    (let [document (request "GET" (object-path bucket key) nil {:alt "media" :generation (:generation metadata)})]
      (when-not document (throw (ex-info "GCS generation disappeared" {})))
      (when (and max-bytes (or (not (map? document)) (> (alength (.getBytes (json/generate-string document) "UTF-8")) max-bytes)))
        (throw (ex-info "invalid GCS document" {})))
      {:document document :etag (:generation metadata)}))))
(defn put-object [request bucket key document generation]
  (request "POST" (str "upload/storage/v1/b/" (encode bucket) "/o") document {:uploadType "media" :name key :ifGenerationMatch generation}))
