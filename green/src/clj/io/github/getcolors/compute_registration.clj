(ns io.github.getcolors.compute-registration
  "Read-only paginated registration collision checks; no adoption or deletion."
  (:require [cheshire.core :as json] [clojure.java.io :as io] [clojure.string :as str]
            [io.github.getcolors.compute-journal :as journal])
  (:import [java.net URI URL Proxy HttpURLConnection URLDecoder URLEncoder]
           [java.nio ByteBuffer] [java.nio.charset StandardCharsets]))
(def descriptors (json/parse-string (slurp (io/resource "colors_compute/registration-preflight.json")) true))
(defn- require-valid [value] (when-not value (throw (ex-info "SSH registration preflight failed" {}))))
(defn- missing? [value] (or (not (string? value)) (str/blank? value) (= "REPLACE_ME" (str/upper-case (str/trim value)))))
(defn- identifier [value]
  (cond (and (string? value) (not (missing? value))) value
        (and (number? value) (<= 1 value 9007199254740991) (== value (Math/floor (double value)))) (str (long value))
        :else (require-valid false)))
(defn- public [value] (require-valid (and (string? value) (>= (count (str/split (str/trim value) #"\s+")) 2))) (str/join " " (take 2 (str/split (str/trim value) #"\s+"))))
(defn- http-get [url headers]
  (let [connection ^HttpURLConnection (.openConnection (URL. url) Proxy/NO_PROXY)]
    (try
      (.setInstanceFollowRedirects connection false) (.setConnectTimeout connection 30000) (.setReadTimeout connection 30000)
      (doseq [[key value] headers] (.setRequestProperty connection key value))
      (require-valid (= 200 (.getResponseCode connection)))
      (with-open [stream (.getInputStream connection)]
        (let [bytes (.readNBytes stream 2097153)] (require-valid (<= (alength bytes) 2097152)) bytes))
      (finally (.disconnect connection)))))
(defn- encode [value] (URLEncoder/encode (str value) "UTF-8"))
(defn- query-url [descriptor field value] (str (:endpoint descriptor) "?per_page=" (:per_page descriptor) (when field (str "&" field "=" (encode value)))))
(defn- valid-url! [url descriptor visited]
  (let [parsed (URI. url) origin (URI. (:endpoint descriptor))
        pairs (mapv (fn [part] (let [pair (str/split part #"=" -1)] (require-valid (= 2 (count pair))) (mapv #(URLDecoder/decode % "UTF-8") pair)))
                    (str/split (or (.getRawQuery parsed) "") #"&" -1))
        allowed (if (contains? #{"page" "url"} (:pagination descriptor)) #{"per_page" "page"} #{"per_page" "cursor"})]
    (require-valid (and (<= (count url) 4096) (= "https" (.getScheme parsed)) (= (.getRawAuthority origin) (.getRawAuthority parsed))
                        (= (.getPath origin) (.getPath parsed)) (nil? (.getFragment parsed)) (nil? (.getUserInfo parsed))
                        (= (count pairs) (count (into {} pairs))) (every? allowed (map first pairs)) (not (contains? visited url))))))
(defn registration-preflight
  ([opts key-mode] (registration-preflight opts key-mode nil nil (into {} (System/getenv)) http-get))
  ([opts key-mode ownership public-key environment] (registration-preflight opts key-mode ownership public-key environment http-get))
  ([opts key-mode ownership public-key environment http]
   (try
     (require-valid (and (contains? #{"managed" "external"} key-mode) (string? (:profile opts)) (re-matches #"[a-zA-Z0-9][a-zA-Z0-9_-]{0,62}" (:profile opts))))
     (let [provider (:provider-compute opts) descriptor (when (string? provider) (get descriptors (keyword provider)))]
       (if (or (= key-mode "external") (nil? descriptor) (contains? #{:build "build"} (:green/event opts)) (true? (:green/dry-run opts))) {:status "skipped"}
           (let [owned-id (when (some? ownership)
                            (require-valid (and (map? ownership) (= #{:provider :scope :id} (set (keys ownership))) (= provider (:provider ownership)) (= (:scope descriptor) (:scope ownership))))
                            (identifier (:id ownership)))
                 material (when (some? public-key) (public public-key)) token (get environment (:token descriptor))
                 _ (require-valid (and (not (missing? token)) (not (re-find #"[\r\n]" token))))
                 headers {"Authorization" (str "Bearer " token) "Accept" "application/json"}]
             (loop [url (query-url descriptor (when (= "page" (:pagination descriptor)) "page") 1) visited #{} identifiers #{} matching [] owned-seen false]
               (require-valid (< (count visited) 1000)) (valid-url! url descriptor visited)
               (let [body (http url headers)]
                 (require-valid (and (bytes? body) (<= (alength body) 2097152)))
                 (let [data (journal/parse-one (str (.decode (.newDecoder StandardCharsets/UTF_8) (ByteBuffer/wrap body))))
                       _ (require-valid (and (map? data) (vector? (:ssh_keys data))))
                       found (reduce (fn [{:keys [identifiers matching owned-seen]} entry]
                                       (require-valid (and (map? entry) (string? (:name entry))))
                                       (let [id (identifier (:id entry)) material (public (get entry (keyword (:public descriptor))))]
                                         (require-valid (and (not (contains? identifiers id)) (< (count identifiers) 100000)))
                                         {:identifiers (conj identifiers id) :owned-seen (or owned-seen (= id owned-id))
                                          :matching (cond-> matching (= (:profile opts) (:name entry)) (conj [id material]))}))
                                     {:identifiers identifiers :matching matching :owned-seen owned-seen} (:ssh_keys data))
                       kind (:pagination descriptor)
                       next-value (if (= kind "url")
                                    (let [links (get data :links {}) pages (get links :pages {})]
                                      (require-valid (and (map? links) (map? pages))) (:next pages))
                                    (let [section (get-in data [:meta (if (= kind "page") :pagination :links)]) field (if (= kind "page") :next_page :next)]
                                      (require-valid (and (map? section) (contains? section field))) (get section field)))]
                   (if (or (nil? next-value) (and (= kind "cursor") (= "" next-value)))
                     (do
                       (require-valid (or (nil? owned-id) (:owned-seen found)))
                       (doseq [[id key] (:matching found)]
                         (if (= id owned-id) (require-valid (or (nil? material) (= material key)))
                             (throw (ex-info (if (and material (= material key)) "unowned SSH registration; verify whether hosts survive before explicit recovery"
                                                "foreign SSH registration; do not delete it; investigate or change profile") {:registration/safe true}))))
                       {:status "checked"})
                     (let [next-url (if (= kind "page")
                                      (do (require-valid (and (number? next-value) (<= 1 next-value 9007199254740991) (== next-value (Math/floor (double next-value))))) (query-url descriptor "page" (long next-value)))
                                      (do (require-valid (and (string? next-value) (not (empty? next-value)))) (if (= kind "url") next-value (query-url descriptor "cursor" next-value))))]
                       (recur next-url (conj visited url) (:identifiers found) (:matching found) (:owned-seen found))))))))))
     (catch InterruptedException error (throw error))
     (catch Exception error (if (:registration/safe (ex-data error)) (throw error) (throw (ex-info "SSH registration preflight failed" {})))))))
