(ns io.github.getcolors.compute-managed-access
  "Reviewed managed outputs and a private, fixed kubeconfig sink."
  (:require [cheshire.core :as json] [clojure.java.io :as io] [clojure.string :as str]
            [yamlstar.core :as yaml]
            [io.github.getcolors.compute-runtime :as runtime]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-request :as request])
  (:import [java.net URI] [java.nio ByteBuffer] [java.nio.charset StandardCharsets CodingErrorAction]
           [java.nio.file Files Path LinkOption StandardCopyOption] [java.util Base64 IdentityHashMap]))
(defn- safe? [v] (and (string? v) (boolean (re-matches #"[A-Za-z0-9][A-Za-z0-9_-]{0,62}" v))))
(defn- require-valid [v] (when-not v (throw (ex-info "invalid managed outputs" {}))))
(defn managed-kubeconfig-path [opts]
  (when-not (and (string? (:workdir opts)) (not (str/blank? (:workdir opts))) (safe? (:profile opts)))
    (throw (ex-info "invalid managed access path" {})))
  (str (.normalize (.toAbsolutePath (.toPath (io/file (:workdir opts) (:profile opts) "kubeconfig"))))))
(defn public-params [value provider]
  (let [required #{:provider :kind :name :cluster_id :endpoint} optional #{:pod_cidr :service_cidr}]
    (require-valid (and (map? value) (every? #(contains? value %) required) (every? (into required optional) (keys value))))
    (require-valid (and (= provider (:provider value)) (= "managed-kubernetes" (:kind value)) (safe? (:name value))
                        (string? (:cluster_id value)) (re-matches #"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}" (:cluster_id value)) (string? (:endpoint value))))
    (let [uri (URI. (:endpoint value))]
      (require-valid (and (= "https" (.getScheme uri)) (seq (.getHost uri)) (nil? (.getUserInfo uri)) (nil? (.getFragment uri)) (nil? (.getQuery uri)))))
    (doseq [field optional :when (contains? value field)] (require-valid (string? (get value field))) (request/cidr (get value field) false))
    value))
(defn- safe-kubeconfig! [content params]
  (let [config (yaml/load content) seen (IdentityHashMap.)
        forbidden #{"exec" "auth-provider" "tokenFile" "client-certificate" "client-key" "certificate-authority" "proxy-url" "tls-server-name"}]
    (require-valid (and (map? config) (= "Config" (get config "kind")) (= "v1" (get config "apiVersion"))))
    (let [clusters (get config "clusters") cluster (get (first clusters) "cluster")]
      (require-valid (and (vector? clusters) (= 1 (count clusters)) (map? cluster)
                          (string? (get cluster "server")) (= (str/replace (get cluster "server") #"/+$" "") (str/replace (:endpoint params) #"/+$" ""))
                          (false? (get cluster "insecure-skip-tls-verify" false)))))
    (let [contexts (get config "contexts") users (get config "users")
          context (first contexts) user (first users)
          cluster-name (get-in config ["clusters" 0 "name"]) context-name (get context "name") user-name (get user "name")
          credentials (get user "user") nonblank? #(and (string? %) (not (str/blank? %)))]
      (require-valid (and (vector? contexts) (= 1 (count contexts)) (vector? users) (= 1 (count users))
                          (map? context) (map? user) (map? (get context "context")) (map? credentials)
                          (every? nonblank? [cluster-name context-name user-name])
                          (= (get config "current-context") context-name)
                          (= (get-in context ["context" "cluster"]) cluster-name)
                          (= (get-in context ["context" "user"]) user-name)
                          (or (nonblank? (get credentials "token"))
                              (every? #(nonblank? (get credentials %)) ["client-certificate-data" "client-key-data"])))))
    (loop [pending [config]]
      (when (seq pending)
        (let [value (peek pending) pending (pop pending)]
          (if (or (map? value) (sequential? value))
            (do
              (require-valid (and (not (.containsKey seen value)) (< (.size seen) 10000)))
              (.put seen value true)
              (when (map? value) (require-valid (every? #(and (string? %) (not (forbidden %))) (keys value))))
              (recur (into pending (if (map? value) (vals value) value))))
            (recur pending)))))))
(defn access-decoder
  ([opts] (access-decoder opts (into {} (System/getenv))))
  ([opts environment]
  (let [content (atom nil)
        credential-keys (concat (keys (get-in compute/registry [:compute (keyword (:provider-compute opts)) :tofu-env]))
                     (when (= "r2" (:provider-backend opts)) [:r2-access-key-id :r2-secret-access-key]))
        secrets (filter #(and (string? %) (seq %)) (map #(get environment (str "COLORS_PAR_" (str/replace (str/upper-case (name %)) "-" "_"))) credential-keys))]
    {:decode
     (fn [text]
       (let [state (journal/parse-one text) outputs (:outputs state) params (get-in outputs [:params :value]) entry (:kubeconfig_b64 outputs)]
         (require-valid (and (runtime/valid-state? state) (= #{:params :kubeconfig_b64} (set (keys outputs)))
                             (map? (:params outputs)) (false? (get-in outputs [:params :sensitive] false))
                             (map? entry) (true? (:sensitive entry)) (string? (:value entry)) (<= (count (:value entry)) 2796204) (zero? (mod (count (:value entry)) 4))))
         (public-params params (:provider-compute opts))
         (let [raw (.decode (Base64/getDecoder) ^String (:value entry))
               decoder (doto (.newDecoder StandardCharsets/UTF_8) (.onMalformedInput CodingErrorAction/REPORT) (.onUnmappableCharacter CodingErrorAction/REPORT))
               text (str (.decode decoder (ByteBuffer/wrap raw)))]
           (require-valid (and (= (.encodeToString (Base64/getEncoder) raw) (:value entry)) (pos? (alength raw)) (<= (alength raw) 2097152) (not (str/includes? text "\u0000")) (not (str/starts-with? text "\uFEFF"))))
           (require-valid (not-any? (fn [secret] (let [escaped (json/generate-string secret)] (or (str/includes? text secret) (str/includes? text (subs escaped 1 (dec (count escaped))))))) secrets))
           (safe-kubeconfig! text params)
           (reset! content raw)
           {:params params})))
     :write
     (fn []
       (require-valid (some? @content))
       (let [target (.toPath (io/file (managed-kubeconfig-path opts)))
             ancestors (loop [path target result []] (if path (recur (.getParent path) (conj result path)) result))]
         (doseq [path ancestors] (require-valid (not (Files/isSymbolicLink path))))
         (Files/createDirectories (.getParent target) (journal/attrs "rwx------"))
         (when (Files/exists target (make-array LinkOption 0))
           (require-valid (and (Files/isRegularFile target (into-array LinkOption [LinkOption/NOFOLLOW_LINKS]))
                               (= (Files/getOwner target (make-array LinkOption 0)) (Files/getOwner (.getParent target) (make-array LinkOption 0))))))
         (let [temporary (Files/createTempFile (.getParent target) ".kubeconfig-" "" (journal/attrs "rw-------"))]
           (try
             (with-open [stream (java.io.FileOutputStream. (.toFile temporary))]
               (.write stream ^bytes @content) (.force (.getChannel stream) true))
             (Files/move temporary target (into-array StandardCopyOption [StandardCopyOption/ATOMIC_MOVE StandardCopyOption/REPLACE_EXISTING]))
             (str target)
             (finally (Files/deleteIfExists temporary) (reset! content nil))))))})))
