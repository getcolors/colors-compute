(ns io.github.getcolors.compute-local-test
  (:require [clojure.test :refer [deftest is]]
            [cheshire.core :as json]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-local :as local]
            [io.github.getcolors.compute-coordination :as coordination]
            [io.github.getcolors.compute-journal :as journal]
            [io.github.getcolors.compute-execution :as execution]
            [io.github.getcolors.compute-execution-test :as fixture]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files LinkOption]
           [java.nio.file.attribute PosixFilePermissions]))
(defn opts [directory] {:profile "demo" :provider-compute "aws" :provider-backend "local" :local-state-dir (str directory)})
(defn temp-dir [] (Files/createTempDirectory "compute-local-test-" (local/attrs "rwx------")))
(defn mode [path] (PosixFilePermissions/toString (Files/getPosixFilePermissions (local/path path) (make-array LinkOption 0))))
(deftest local-contract
  (doseq [value ["relative" "/trailing/" "/a//b" "/a/../b" "/./a" "/a\\b" (str "/a" (char 0))]]
    (is (not (compute/local-state-dir? value)))
    (is (thrown? Exception (compute/backend-plan (opts value) "demo/compute/shared.tfstate"))))
  (is (= {:config {:terraform {:backend {:local {:path "/demo/compute/shared.tfstate"}}}} :credential_bindings {} :environment {}}
         (compute/backend-plan (opts "/") "demo/compute/shared.tfstate")))
  (is (coordination/identity? (journal/journal-identity (opts "/private/state")))))
(deftest journal-cas-and-errors
  (let [directory (temp-dir) options (opts directory) identity (journal/journal-identity options)
        path (str directory "/demo/compute/coordination.json") lock (local/path (str path ".lock"))
        intent (coordination/coordination {:status "absent"} identity {:type "acquire" :run_id "run-1" :write_id "write-1" :target_etag nil})]
    (try
      (is (= {:status "absent"} (journal/journal-get options {})))
      (let [written (journal/journal-put options intent {}) read (journal/journal-get options {})]
        (is (= "written" (:status written)))
        (is (= (:etag written) (:etag read) (local/etag (Files/readAllBytes (local/path path)))))
        (is (= "rw-------" (mode path)))
        (is (= "rwx------" (mode (str directory "/demo/compute"))))
        (is (= {:status "conflict"} (journal/journal-put options intent {})))
        (is (= {:status "conflict"} (journal/journal-put options (assoc intent :condition {:if_match "stale"}) {})))
        (Files/createDirectory lock (local/attrs "rwx------"))
        (is (= {:status "conflict"} (journal/journal-put options intent {})))
        (Files/delete lock)
        (let [release (coordination/coordination read identity {:type "release" :run_id "run-1" :write_id "write-2" :target_etag (:etag read)})
              updated (journal/journal-put options release {})]
          (is (= "written" (:status updated)))
          (is (not= (:etag written) (:etag updated)))
          (is (= {:status "conflict"} (journal/journal-put options release {}))))
        (spit path "{} {}")
        (is (= {:status "error"} (journal/journal-get options {})))
        (spit path (apply str (repeat 2097153 "x")))
        (is (= {:status "error"} (journal/journal-get options {}))))
      (finally (journal/cleanup! directory)))))
(deftest local-presence-refuses-symlinks-and-directories
  (let [directory (temp-dir) options (opts directory) key "demo/compute/shared.tfstate"
        path (:path (compute/backend-settings options key))]
    (try
      (is (= {:status "absent"} (execution/state-presence options key {})))
      (local/prepare! path)
      (Files/createSymbolicLink (local/path path) (local/path "/missing-local-state-target") (make-array java.nio.file.attribute.FileAttribute 0))
      (is (= {:status "error"} (execution/state-presence options key {})))
      (is (= {:status "error"} (runtime/read-state options key {} (fn [& _] (throw (AssertionError. "must not execute"))))))
      (Files/delete (local/path path))
      (Files/createDirectory (local/path path) (local/attrs "rwx------"))
      (is (= {:status "error"} (execution/state-presence options key {})))
      (finally (journal/cleanup! directory)))))
(deftest create-keeps-state-outside-temporary-workdir
  (let [directory (temp-dir) options (assoc (opts directory) :profile "example")
        key "example/compute/shared.tfstate" path (:path (compute/backend-settings options key)) calls (atom [])
        runner (fn [argv cwd _ _]
                 (swap! calls conj {:argv argv :cwd cwd})
                 (is (= path (get-in (json/parse-string (slurp (str cwd "/backend.tf.json")) true) [:terraform :backend :local :path])))
                 (case (second argv)
                   "show" {:exit 0 :out fixture/valid-plan}
                   "apply" (do (spit path (fixture/state-text "aws")) {:exit 0 :out ""})
                   "state" {:exit 0 :out (slurp path)}
                   {:exit 0 :out ""}))]
    (try
      (is (= "ready" (:status (execution/converge-state options key fixture/documents "create" {:status "absent"} {} runner))))
      (is (= ["init" "plan" "show" "apply" "state"] (mapv #(second (:argv %)) @calls)))
      (is (= {:status "present"} (execution/state-presence options key {})))
      (is (every? #(not (.exists (java.io.File. (:cwd %)))) @calls))
      (finally (journal/cleanup! directory)))))
(deftest refuses-linked-ancestors-and-protects-owned-directories
  (let [directory (temp-dir) target (temp-dir) options (opts directory)
        key "demo/compute/shared.tfstate" profile (.resolve directory "demo")]
    (try
      (Files/createSymbolicLink profile target (make-array java.nio.file.attribute.FileAttribute 0))
      (is (= {:status "error"} (execution/state-presence options key {})))
      (is (= {:status "error"} (journal/journal-get options {})))
      (Files/delete profile)
      (Files/createDirectory profile (local/attrs "rwxr-xr-x"))
      (Files/setPosixFilePermissions directory (PosixFilePermissions/fromString "rwxr-xr-x"))
      (local/private-owned-directory! (str directory) (.resolve profile "compute"))
      (is (= "rwx------" (mode (str profile))))
      (is (= "rwxr-xr-x" (mode (str directory))))
      (finally (journal/cleanup! directory) (journal/cleanup! target)))))

(deftest default-local-directory
  (let [options (dissoc (opts "/override") :local-state-dir)
        expected (str (System/getenv "HOME") "/.local/state/colors")]
    (is (= expected (compute/local-state-directory options)))
    (is (= (str expected "/demo/compute/shared.tfstate")
           (get-in (compute/backend-plan options "demo/compute/shared.tfstate") [:config :terraform :backend :local :path])))
    (is (= expected (get-in (journal/journal-identity options) [:backend :path])))
    (is (= "/home/test/.local/state/colors" (compute/local-state-directory options "/home/test")))
    (is (= "/.local/state/colors" (compute/local-state-directory options "/")))
    (is (= "/override" (compute/local-state-directory (opts "/override") nil)))
    (doseq [home [nil "" "relative" "/home/../user"]]
      (is (thrown? Exception (compute/local-state-directory options home))))
    (doseq [value [nil "" "REPLACE_ME"]]
      (is (thrown-with-msg? Exception #":local-state-dir is required"
                           (compute/local-state-directory (assoc options :local-state-dir value) "/home/test"))))))
