(ns io.github.getcolors.compute-local
  "Private persistent files for the local OpenTofu backend."
  (:require [clojure.java.io :as io])
  (:import [java.nio.file Files Path LinkOption NoSuchFileException FileAlreadyExistsException StandardCopyOption]
           [java.nio.file.attribute PosixFilePermissions FileAttribute BasicFileAttributes]
           [java.security MessageDigest]))
(defn attrs [mode]
  (into-array FileAttribute [(PosixFilePermissions/asFileAttribute (PosixFilePermissions/fromString mode))]))
(defn path [value] (.toPath (io/file value)))
(defn- check-ancestors! [^Path directory]
  (when directory
    (check-ancestors! (.getParent directory))
    (try
      (let [attributes (Files/readAttributes directory BasicFileAttributes (into-array LinkOption [LinkOption/NOFOLLOW_LINKS]))]
        (when-not (.isDirectory attributes) (throw (ex-info "invalid local directory" {}))))
      (catch NoSuchFileException _ nil))))
(defn presence [value]
  (try
    (let [p (path value) _ (check-ancestors! (.getParent p)) attributes (Files/readAttributes p BasicFileAttributes (into-array LinkOption [LinkOption/NOFOLLOW_LINKS]))]
      (if (and (.isRegularFile attributes) (Files/isReadable p)) {:status "present"} {:status "error"}))
    (catch NoSuchFileException _ {:status "absent"})))
(defn private-directory! [^Path directory]
  (when directory
    (check-ancestors! (.getParent directory))
    (try
      (let [attributes (Files/readAttributes directory BasicFileAttributes (into-array LinkOption [LinkOption/NOFOLLOW_LINKS]))]
        (when-not (.isDirectory attributes) (throw (ex-info "invalid local directory" {}))))
      (catch NoSuchFileException _
        (private-directory! (.getParent directory))
        (try (Files/createDirectory directory (attrs "rwx------"))
             (catch FileAlreadyExistsException _ (private-directory! directory)))))))
(defn private-owned-directory! [root ^Path directory]
  (private-directory! directory)
  (loop [current directory]
    (when (and current (not= current (path root)))
      (Files/setPosixFilePermissions current (PosixFilePermissions/fromString "rwx------"))
      (recur (.getParent current)))))
(defn prepare! [value]
  (let [p (path value)]
    (private-directory! (.getParent p))
    (case (:status (presence value))
      "present" (Files/setPosixFilePermissions p (PosixFilePermissions/fromString "rw-------"))
      "absent" nil
      (throw (ex-info "invalid local state file" {})))))
(defn etag [bytes]
  (apply str (map #(format "%02x" (bit-and 255 %)) (.digest (MessageDigest/getInstance "SHA-256") bytes))))
(defn read-bytes [value limit]
  (when-not (= {:status "present"} (presence value)) (throw (ex-info "invalid local file" {})))
  (with-open [stream (io/input-stream value)]
    (let [bytes (.readNBytes stream (inc limit))]
      (when (> (alength bytes) limit) (throw (ex-info "local document too large" {}))) bytes)))
(defn write-atomic! [value text]
  (let [p (path value) temporary (Files/createTempFile (.getParent p) ".compute-" ".tmp" (attrs "rw-------"))]
    (try
      (with-open [stream (java.io.FileOutputStream. (.toFile temporary))]
        (.write stream (.getBytes text java.nio.charset.StandardCharsets/UTF_8))
        (.force (.getChannel stream) true))
      (Files/move temporary p (into-array StandardCopyOption [StandardCopyOption/ATOMIC_MOVE StandardCopyOption/REPLACE_EXISTING]))
      (finally (Files/deleteIfExists temporary)))))
