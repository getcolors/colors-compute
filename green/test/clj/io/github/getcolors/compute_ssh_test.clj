(ns io.github.getcolors.compute-ssh-test
  (:require [clojure.test :refer [deftest is]]
            [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute-ssh :as ssh]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files LinkOption]
           [java.nio.file.attribute PosixFilePermissions]))

(def opts {:profile "demo" :provider-compute "aws"})
(defn with-home [f]
  (let [root (Files/createTempDirectory "colors-ssh-test-" (make-array java.nio.file.attribute.FileAttribute 0))]
    (try (f root {"HOME" (str root) "PATH" "/usr/bin:/bin"})
         (finally
           (with-open [paths (Files/walk root (make-array java.nio.file.FileVisitOption 0))]
             (doseq [path (reverse (sort-by #(.getNameCount %) (iterator-seq (.iterator paths))))]
               (Files/deleteIfExists path)))))))
(defn mode [path]
  (PosixFilePermissions/toString (Files/getPosixFilePermissions (.toPath (io/file path)) (make-array LinkOption 0))))
(defn prepare [options env]
  (ssh/prepare-keypair! options {:status "fresh"} env (constantly true) (constantly true)))
(defn ownership [result] {:status "prepared" :fingerprint (:fingerprint result)})

(deftest actual-keygen-records-before-work-and-reuses-pair
  (with-home
    (fn [home env]
      (let [events (atom [])
            result (ssh/prepare-keypair! opts {:status "fresh"} env
                     (fn [] (is (not (.exists (io/file (str home) ".ssh/demo")))) (swap! events conj :intent) true)
                     (fn [fingerprint]
                       (is (.exists (io/file (str home) ".ssh/demo")))
                       (is (str/starts-with? fingerprint "SHA256:"))
                       (swap! events conj :prepared) true))]
        (is (= [:intent :prepared] @events))
        (is (= "rwx------" (mode (io/file (str home) ".ssh"))))
        (is (= "rw-------" (mode (:private_key_path result))))
        (is (= "rw-------" (mode (:public_key_path result))))
        (is (not (str/includes? (pr-str result) "OPENSSH PRIVATE KEY")))
        (is (str/includes? (:public_key result) "demo managed by Colors"))
        (let [verified (runtime/run-command ["ssh-keygen" "-lf" (:public_key_path result) "-E" "sha256"] (str home) env 30000)]
          (is (= 0 (:exit verified)))
          (is (str/includes? (:out verified) (:fingerprint result))))
        (is (= result (ssh/prepare-keypair! opts (ownership result) env
                        (fn [] (throw (AssertionError. "must reuse")))
                        (fn [_] (throw (AssertionError. "must reuse"))))))))))

(deftest unconfirmed-ownership-never-adopts-generated-files
  (with-home
    (fn [home env]
      (is (thrown-with-msg? Exception #"SSH key ownership update failed"
                            (ssh/prepare-keypair! opts {:status "fresh"} env (constantly false) (constantly true))))
      (is (not (.exists (io/file (str home) ".ssh/demo"))))
      (is (not (.exists (io/file (str home) ".ssh/.demo.colors-key.lock"))))
      (is (thrown-with-msg? Exception #"SSH key ownership update failed"
                            (ssh/prepare-keypair! opts {:status "fresh"} env (constantly true) (constantly false))))
      (let [private (io/file (str home) ".ssh/demo") before (slurp private)]
        (is (.exists private))
        (is (thrown-with-msg? Exception #"unowned SSH key files exist"
                              (ssh/prepare-keypair! opts {:status "fresh"} env (constantly true) (constantly true))))
        (is (= before (slurp private)))))))

(deftest prepared-ownership-refuses-missing-or-mismatched-pairs
  (with-home
    (fn [_ env]
      (let [result (prepare opts env) other (prepare (assoc opts :profile "other") env)]
        (spit (:public_key_path result) (:public_key other))
        (is (thrown-with-msg? Exception #"SSH keypair is inconsistent"
                              (ssh/prepare-keypair! opts (ownership result) env (constantly true) (constantly true))))
        (Files/delete (.toPath (io/file (:public_key_path result))))
        (is (thrown-with-msg? Exception #"owned SSH keypair is missing"
                              (ssh/prepare-keypair! opts (ownership result) env (constantly true) (constantly true))))
        (is (.exists (io/file (:private_key_path result))))))))

(deftest cleanup-requires-complete-destruction-and-is-idempotent
  (with-home
    (fn [home env]
      (let [result (prepare opts env) recorded (ownership result)
            known (io/file (str home) ".ssh/demo.known_hosts")
            unrelated (io/file (str home) ".ssh/unrelated")]
        (spit known "owned-host")
        (spit unrelated "keep")
        (is (thrown-with-msg? Exception #"complete resource destruction"
                              (ssh/cleanup-keypair! opts recorded {:all_resources_destroyed false} env)))
        (is (.exists (io/file (:private_key_path result))))
        ;; Simulate interruption after one file was removed; remaining key still proves ownership.
        (Files/delete (.toPath (io/file (:public_key_path result))))
        (is (= {:mode "managed" :cleaned true}
               (ssh/cleanup-keypair! opts recorded {:all_resources_destroyed true :known_hosts_owned true} env)))
        (is (not (.exists known)))
        (is (not (.exists (io/file (:private_key_path result)))))
        (is (= "keep" (slurp unrelated)))
        (is (.isDirectory (io/file (str home) ".ssh")))
        (is (= {:mode "managed" :cleaned true}
               (ssh/cleanup-keypair! opts recorded {:all_resources_destroyed true :known_hosts_owned true} env)))))))

(deftest external-and-planning-modes-never-touch-local-files
  (with-home
    (fn [home env]
      (let [external (assoc opts :aws-ssh-authorized-keys "/operator/existing.pub" :ssh-private-key-path "/operator/existing")
            never (fn [& _] (throw (AssertionError. "must not run")))]
        (is (= {:mode "external" :setting "aws-ssh-authorized-keys" :reference "/operator/existing.pub" :private_key_path "/operator/existing"}
               (ssh/prepare-keypair! external {:status "error"} env never never never)))
        (is (= "external" (:mode (ssh/cleanup-keypair! external {:status "error"} {} env never))))
        (is (= ssh/placeholder-public
               (:public_key (ssh/prepare-keypair! (assoc opts :green/event :build) {:status "error"} env never never never))))
        (is (= {:mode "managed" :cleaned false :planned true}
               (ssh/cleanup-keypair! (assoc opts :green/dry-run true) {:status "error"} {} env never)))
        (is (not (.exists (io/file (str home) ".ssh"))))
        (doseq [invalid [nil "" "REPLACE_ME" [] [nil]]]
          (is (thrown-with-msg? Exception #"invalid external SSH key reference"
                                (ssh/prepare-keypair! (assoc opts :aws-ssh-authorized-keys invalid) {:status "fresh"} env never never never))))))))

(deftest symlink-and-uncertain-ownership-fail-without-adoption
  (with-home
    (fn [home env]
      (is (thrown-with-msg? Exception #"SSH key ownership uncertain"
                            (ssh/prepare-keypair! opts {:status "error"} env (constantly true) (constantly true))))
      (is (not (.exists (io/file (str home) ".ssh"))))
      (let [target (.resolve home "foreign") link (.resolve home ".ssh")]
        (Files/createDirectory target (make-array java.nio.file.attribute.FileAttribute 0))
        (Files/createSymbolicLink link target (make-array java.nio.file.attribute.FileAttribute 0))
        (is (thrown-with-msg? Exception #"unsafe SSH key path"
                              (ssh/prepare-keypair! opts {:status "fresh"} env (constantly true) (constantly true))))
        (is (Files/isSymbolicLink link))))))

(deftest callback-cancellation-keeps-key-and-runner-errors-are-redacted
  (with-home
    (fn [home env]
      (is (thrown? InterruptedException
                   (ssh/prepare-keypair! opts {:status "fresh"} env (constantly true)
                     (fn [_] (throw (InterruptedException. "cancelled"))))))
      (is (.exists (io/file (str home) ".ssh/demo")))))
  (with-home
    (fn [_ env]
      (is (thrown-with-msg? Exception #"^SSH key operation failed$"
                            (ssh/prepare-keypair! opts {:status "fresh"} env (constantly true) (constantly true)
                              (fn [& _] (throw (ex-info "private process detail" {})))))))))

(deftest cross-backend-concurrent-fresh-prepare-has-one-local-generator
  (with-home
    (fn [home env]
      (let [entered (promise) proceed (promise) generated (atom 0)
            runner (fn [argv cwd environment timeout]
                     (when (some #{"-q"} argv) (swap! generated inc))
                     (runtime/run-command argv cwd environment timeout))
            first-call (future
                         (ssh/prepare-keypair! opts {:status "fresh"} env
                           (fn [] (deliver entered true) (deref proceed 3000 false))
                           (constantly true) runner))]
        (is (deref entered 3000 false))
        (is (= "rw-------" (mode (io/file (str home) ".ssh/.demo.colors-key.lock"))))
        (is (thrown-with-msg? Exception #"SSH key reservation exists"
                              (ssh/prepare-keypair! (assoc opts :provider-backend "r2") {:status "fresh"} env
                                (fn [] (throw (AssertionError. "second process must not record"))) (constantly true) runner)))
        (deliver proceed true)
        (is (= "managed" (:mode (deref first-call 3000 nil))))
        (is (= 1 @generated))
        (is (not (.exists (io/file (str home) ".ssh/.demo.colors-key.lock"))))))))

(deftest stale-reservation-is-never-deleted-or-adopted
  (with-home
    (fn [home env]
      (let [directory (io/file (str home) ".ssh") reservation (io/file directory ".demo.colors-key.lock")]
        (.mkdir directory)
        (spit reservation "existing-reservation")
        (is (thrown-with-msg? Exception #"SSH key reservation exists"
                              (ssh/prepare-keypair! opts {:status "fresh"} env
                                (fn [] (throw (AssertionError. "must not record"))) (constantly true))))
        (is (= "existing-reservation" (slurp reservation)))
        (is (not (.exists (io/file directory "demo"))))))))

(deftest cleanup-respects-local-reservation-and-absent-directory
  (with-home
    (fn [home env]
      (is (= {:mode "managed" :cleaned true}
             (ssh/cleanup-keypair! opts {:status "fresh"} {:all_resources_destroyed true} env)))
      (is (not (.exists (io/file (str home) ".ssh"))))
      (let [directory (io/file (str home) ".ssh") reservation (io/file directory ".demo.colors-key.lock")]
        (.mkdir directory)
        (spit reservation "another-owner")
        (is (thrown-with-msg? Exception #"SSH key reservation exists"
                              (ssh/cleanup-keypair! opts {:status "fresh"} {:all_resources_destroyed true} env)))
        (is (= "another-owner" (slurp reservation)))))))
