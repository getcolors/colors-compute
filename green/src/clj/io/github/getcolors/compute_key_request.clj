(ns io.github.getcolors.compute-key-request
  (:require [clojure.string :as str]
            [io.github.getcolors.compute-deployment-request :as deployment]
            [io.github.getcolors.compute-ssh :as ssh])
  (:import [java.nio.file Files Paths LinkOption OpenOption StandardOpenOption]
           [java.nio.charset StandardCharsets]
           [java.nio ByteBuffer]
           [java.util Base64]))
(defn- fail [message] (throw (ex-info message {})))
(defn- missing? [value] (or (nil? value) (and (string? value) (or (str/blank? value) (= "REPLACE_ME" (str/upper-case (str/trim value)))))))
(defn- public-key [value]
  (when-not (string? value) (fail "invalid external SSH public key"))
  (let [value (str/trim value) parts (str/split value #"\s+") algorithm (first parts)]
    (when-not (and (>= (count parts) 2) (not (re-find #"[\r\n]" value))
                   (contains? #{"ssh-ed25519" "ssh-rsa" "ecdsa-sha2-nistp256" "ecdsa-sha2-nistp384" "ecdsa-sha2-nistp521"} algorithm))
      (fail "invalid external SSH public key"))
    (try
      (let [blob (.decode (Base64/getDecoder) ^String (second parts)) buffer (ByteBuffer/wrap blob) length (.getInt buffer)]
        (when-not (and (pos? length) (< length (.remaining buffer))) (throw (Exception.)))
        (let [name-bytes (byte-array length)] (.get buffer name-bytes)
          (when-not (= algorithm (String. name-bytes StandardCharsets/UTF_8)) (throw (Exception.)))))
      (catch Exception _ (fail "invalid external SSH public key")))
    value))
(defn key-request
  ([opts prepared] (key-request opts prepared (into {} (System/getenv))))
  ([opts prepared environment]
   (case (:mode prepared)
     "managed" {:mode "managed" :public_key (:public_key prepared)}
     "external"
     (let [provider (:provider-compute opts) recipe (when (string? provider) (get deployment/recipes (keyword provider)))
           _ (when-not recipe (fail "compute provider recipe unavailable"))
           kind (:external_key_kind recipe) reference (:reference prepared) references (if (vector? reference) reference [reference])]
       (if (= kind "ids")
         (do (when-not (and (seq references) (every? #(or (and (string? %) (not (missing? %)))
                                                                         (and (number? %) (< 0 % 9007199254740992) (== % (Math/floor (double %))))) references))
               (fail "invalid external SSH key reference"))
             {:mode "external" :ids references :reference (first references)})
         (do
           (when-not (and (= 1 (count references)) (string? (first references)) (not (missing? (first references))))
             (fail "one external SSH public key is required"))
           {:mode "external" :public_key
            (cond
              (or (contains? #{:build "build"} (:green/event opts)) (true? (:green/dry-run opts))) ssh/placeholder-public
              (= kind "content") (public-key (first references))
              (= kind "public_file")
              (let [filename (first references) filename (if (str/starts-with? filename "~/") (str (get environment "HOME" (System/getProperty "user.home")) "/" (subs filename 2)) filename)
                    path (Paths/get filename (make-array String 0)) nofollow (into-array LinkOption [LinkOption/NOFOLLOW_LINKS])]
                (when-not (and (str/ends-with? filename ".pub") (Files/isRegularFile path nofollow))
                  (fail "external SSH key must name a regular .pub file"))
                (try
                  (with-open [stream (Files/newInputStream path (into-array OpenOption [StandardOpenOption/READ LinkOption/NOFOLLOW_LINKS]))]
                    (let [bytes (.readNBytes stream 65537)]
                      (when (> (alength bytes) 65536) (throw (Exception.)))
                      (public-key (str (.decode (.newDecoder StandardCharsets/UTF_8) (ByteBuffer/wrap bytes))))))
                  (catch Exception _ (fail "invalid external SSH public key file"))))
              :else (fail "unsupported external SSH key reference"))})))
     (fail "invalid compute key request"))))
