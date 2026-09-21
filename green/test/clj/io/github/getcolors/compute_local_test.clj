(ns io.github.getcolors.compute-local-test
  (:require [clojure.test :refer [deftest is]]
            [cheshire.core :as json]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-local :as local]
            [io.github.getcolors.compute-node-test :as node-test])
  (:import [java.nio.file Files LinkOption]
           [java.nio.file.attribute PosixFilePermissions]))
(defn opts [directory] {:profile "demo" :provider-compute "aws" :provider-backend "local" :local-state-dir (str directory)})
(defn temp-dir [] (.toRealPath (Files/createTempDirectory "compute-local-test-" (local/attrs "rwx------")) (make-array LinkOption 0)))
(defn mode [path] (PosixFilePermissions/toString (Files/getPosixFilePermissions (local/path path) (make-array LinkOption 0))))
(deftest local-presence-refuses-symlinks-and-directories
  (let [directory (temp-dir) options (opts directory) key "demo/compute/shared.tfstate"
        path (:path (compute/backend-settings options key))]
    (try
      (is (= {:status "absent"} (local/presence path)))
      (local/prepare! path)
      (Files/createSymbolicLink (local/path path) (local/path "/missing-local-state-target") (make-array java.nio.file.attribute.FileAttribute 0))
      (is (= {:status "error"} (local/presence path)))
      (Files/delete (local/path path))
      (Files/createDirectory (local/path path) (local/attrs "rwx------"))
      (is (= {:status "error"} (local/presence path)))
      (finally (node-test/remove-tree (str directory))))))
(deftest refuses-linked-ancestors-and-protects-owned-directories
  (let [directory (temp-dir) target (temp-dir) options (opts directory)
        key "demo/compute/shared.tfstate" profile (.resolve directory "demo")]
    (try
      (Files/createSymbolicLink profile target (make-array java.nio.file.attribute.FileAttribute 0))
      (is (thrown? Exception (local/presence (:path (compute/backend-settings options key)))))
      (Files/delete profile)
      (Files/createDirectory profile (local/attrs "rwxr-xr-x"))
      (Files/setPosixFilePermissions directory (PosixFilePermissions/fromString "rwxr-xr-x"))
      (local/private-owned-directory! (str directory) (.resolve profile "compute"))
      (is (= "rwx------" (mode (str profile))))
      (is (= "rwxr-xr-x" (mode (str directory))))
      (finally (node-test/remove-tree (str directory)) (node-test/remove-tree (str target))))))
