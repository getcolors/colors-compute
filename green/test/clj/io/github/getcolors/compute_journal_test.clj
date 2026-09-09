(ns io.github.getcolors.compute-journal-test
  (:require [clojure.test :refer [deftest is]]
            [clojure.java.io :as io]
            [cheshire.core :as json]
            [io.github.getcolors.compute-coordination :as coordination]
            [io.github.getcolors.compute-journal :as journal])
  (:import [java.nio.file Files LinkOption]
           [java.nio.file.attribute PosixFilePermissions]))

(def s3 {:profile "demo" :provider-compute "aws" :provider-backend "s3" :s3-bucket "states" :s3-region "eu-west-1"})
(def r2 {:profile "demo" :provider-compute "aws" :provider-backend "r2" :r2-bucket "states" :r2-endpoint "https://example.invalid"})
(def identity {:profile "demo" :provider "aws" :backend {:kind "s3" :bucket "states" :region "eu-west-1"}})
(def intent (coordination/coordination {:status "absent"} identity
              {:type "acquire" :run_id "run-1" :write_id "write-1" :target_etag nil}))
(defn mode [path]
  (PosixFilePermissions/toString (Files/getPosixFilePermissions (.toPath (io/file path)) (make-array LinkOption 0))))
(defn service-error [code operation]
  {:exit 1 :err (str "An error occurred (" code ") when calling the " operation " operation: private detail") :out ""})

(deftest get-classifies-only-exact-service-prefix
  (doseq [[response status] [[(service-error "NoSuchKey" "GetObject") "absent"]
                             [{:exit 1 :err "\naws: [ERROR]: An error occurred (NoSuchKey) when calling the GetObject operation (reached max retries: 0): synthetic response\n"} "absent"]
                             [(service-error "NoSuchBucket" "GetObject") "error"]
                             [(service-error "AccessDenied" "GetObject") "error"]
                             [(service-error "NoSuchKey" "PutObject") "error"]
                             [{:exit 1 :err "404" :out ""} "error"]
                             [{:exit 1 :err "warning\nAn error occurred (NoSuchKey) when calling the GetObject operation:" :out ""} "error"]
                             [{:exit 1 :err "An error occurred (AccessDenied) when calling the GetObject operation: NoSuchKey" :out ""} "error"]
                             [{:exit 0 :err "An error occurred (NoSuchKey) when calling the GetObject operation:" :out ""} "error"]]]
    (let [directory (atom nil)]
      (is (= {:status status}
             (journal/journal-get s3 {} (fn [_ dir _ _] (reset! directory dir) response))))
      (is (not (.exists (io/file @directory)))))))

(deftest private-r2-get-removes-aws-contamination
  (let [directory (atom nil)
        env {"PATH" "/tools" "HOME" "/operator" "AWS_PROFILE" "foreign" "AWS_SECRET_ACCESS_KEY" "foreign-secret"
             "AWS_SESSION_TOKEN" "foreign-token" "AWS_ENDPOINT_URL" "https://foreign.invalid"
             "AWS_CA_BUNDLE" "/trusted/ca.pem" "COLORS_PAR_R2_ACCESS_KEY_ID" "r2-example-access"
             "COLORS_PAR_R2_SECRET_ACCESS_KEY" "r2-example-secret" "COLORS_PAR_UNUSED" "unused"}
        result (journal/journal-get r2 env
                 (fn [argv dir child timeout]
                   (reset! directory dir)
                   (is (= ["aws" "s3api" "get-object" "--bucket" "states" "--key" "demo/compute/coordination.json"] (subvec argv 0 7)))
                   (is (= "rwx------" (mode dir)))
                   (is (= "rw-------" (mode (nth argv 7))))
                   (is (= 120000 timeout))
                   (is (= "/trusted/ca.pem" (get child "AWS_CA_BUNDLE")))
                   (is (= "/operator" (get child "HOME")))
                   (is (not-any? #(contains? child %) ["AWS_PROFILE" "AWS_SECRET_ACCESS_KEY" "AWS_SESSION_TOKEN" "AWS_ENDPOINT_URL" "COLORS_PAR_UNUSED"]))
                   (is (= "1" (get child "AWS_MAX_ATTEMPTS")))
                   (is (= "when_required" (get child "AWS_REQUEST_CHECKSUM_CALCULATION")))
                   (is (= "rw-------" (mode (get child "AWS_SHARED_CREDENTIALS_FILE"))))
                   (is (= "[default]\naws_access_key_id = r2-example-access\naws_secret_access_key = r2-example-secret\n"
                          (slurp (get child "AWS_SHARED_CREDENTIALS_FILE"))))
                   (is (= "" (slurp (get child "AWS_CONFIG_FILE"))))
                   (is (not (clojure.string/includes? (pr-str argv) "r2-example-secret")))
                   (spit (nth argv 7) (json/generate-string {:untrusted "document"}))
                   {:exit 0 :out "{\"ETag\":\"etag-1\"}" :err ""}))]
    (is (= {:status "present" :etag "etag-1" :document {:untrusted "document"}} result))
    (is (not (.exists (io/file @directory))))))

(deftest put-validates-before-call-and-never-retries
  (doseq [bad [nil {} (assoc intent :extra true)
                (assoc-in intent [:document :identity :profile] "other")
                (assoc-in intent [:document :secret] "do-not-emit")
                (assoc intent :condition {:if_match ""})
                (assoc intent :condition {:if_none_match "*" :if_match "etag"})]]
    (is (= {:status "error"}
           (journal/journal-put s3 bad {} (fn [& _] (throw (AssertionError. "must not call")))))))
  (doseq [[response status] [[{:exit 0 :out "{\"ETag\":\"etag-1\"}"} "written"]
                             [(service-error "PreconditionFailed" "PutObject") "conflict"]
                             [(service-error "ConditionalRequestConflict" "PutObject") "conflict"]
                             [{:exit 1 :err "\naws: [ERROR]: An error occurred (PreconditionFailed) when calling the PutObject operation (reached max retries: 0): synthetic response"} "conflict"]
                             [(service-error "PreconditionFailed" "GetObject") "error"]
                             [{:exit -1 :out "" :err "timeout with PreconditionFailed"} "error"]]]
    (let [calls (atom 0) directory (atom nil)
          result (journal/journal-put s3 intent {"AWS_PROFILE" "selected" "AWS_ACCESS_KEY_ID" "ambient"}
                   (fn [argv dir env _]
                     (reset! directory dir) (swap! calls inc)
                     (is (= "selected" (get env "AWS_PROFILE")))
                     (is (= "ambient" (get env "AWS_ACCESS_KEY_ID")))
                     (is (some #{"--if-none-match"} argv))
                     (is (not (some #{"--endpoint-url"} argv)))
                     (let [body (nth argv (inc (.indexOf argv "--body")))]
                       (is (= "rw-------" (mode body)))
                       (is (= (:document intent) (json/parse-string (slurp body) true))))
                     response))]
      (is (= (if (= status "written") {:status status :etag "etag-1"} {:status status}) result))
      (is (= 1 @calls))
      (is (not (.exists (io/file @directory)))))))

(deftest malformed-and-oversized-body-fail-closed
  (doseq [[body metadata] [["" "{\"ETag\":\"e\"}"] ["{}{}" "{\"ETag\":\"e\"}"]
                           ["[]" "{\"ETag\":\"e\"}"] ["{}" "{}"] ["{}" "{\"ETag\":\" \"}"]
                           [(apply str (repeat (inc (* 2 1024 1024)) "x")) "{\"ETag\":\"e\"}"]]]
    (is (= {:status "error"}
           (journal/journal-get s3 {}
             (fn [argv _ _ _] (spit (nth argv 7) body) {:exit 0 :out metadata}))))))

(deftest credential-injection-and-cancellation-refused
  (doseq [credential [nil "" "REPLACE_ME" "line\nbreak" "line\rbreak"]]
    (is (= {:status "error"}
           (journal/journal-get r2 {"COLORS_PAR_R2_ACCESS_KEY_ID" credential "COLORS_PAR_R2_SECRET_ACCESS_KEY" "secret"}
             (fn [& _] (throw (AssertionError. "must not call")))))))
  (let [directory (atom nil)]
    (is (thrown? InterruptedException
                 (journal/journal-get s3 {}
                   (fn [_ dir _ _] (reset! directory dir) (throw (InterruptedException. "cancelled"))))))
    (is (not (.exists (io/file @directory))))))

(deftest update-keeps-etag-as-one-argument
  (let [etag "\"opaque quoted etag\""
        update-intent (assoc intent :condition {:if_match etag})]
    (is (= {:status "written" :etag "next-etag"}
           (journal/journal-put s3 update-intent {}
             (fn [argv _ _ _]
               (is (= etag (nth argv (inc (.indexOf argv "--if-match")))))
               (is (not (some #{"--if-none-match"} argv)))
               {:exit 0 :out "{\"ETag\":\"next-etag\"}"}))))))

(deftest invalid-utf8-and-bound-secret-documents-are-not-returned
  (is (= {:status "error"}
         (journal/journal-get s3 {}
           (fn [argv _ _ _]
             (with-open [stream (io/output-stream (nth argv 7))]
               (.write stream (byte-array [(byte 123) (byte 34) (byte 120) (byte 34) (byte 58)
                                           (byte 34) (unchecked-byte 255) (byte 34) (byte 125)])))
             {:exit 0 :out "{\"ETag\":\"e\"}"}))))
  (is (= {:status "error"}
         (journal/journal-get r2 {"COLORS_PAR_R2_ACCESS_KEY_ID" "bound-access" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "bound-secret"}
           (fn [argv _ _ _]
             (spit (nth argv 7) "{\"credential\":\"bound-secret\"}")
             {:exit 0 :out "{\"ETag\":\"e\"}"})))))

(deftest oversized-write-is-refused-before-runner
  (let [endpoint (str "https://" (apply str (repeat (* 2 1024 1024) "a")))
        backend {:kind "r2" :bucket "states" :region "auto" :endpoint endpoint}
        large-intent (assoc-in intent [:document :identity :backend] backend)]
    (is (= {:status "error"}
           (journal/journal-put (assoc r2 :r2-endpoint endpoint) large-intent
             {"COLORS_PAR_R2_ACCESS_KEY_ID" "r2-example-access" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "r2-example-secret"}
             (fn [& _] (throw (AssertionError. "must not call"))))))))

(deftest cleanup-failure-does-not-report-success
  (let [original @#'journal/cleanup!]
    (is (= {:status "error"}
           (with-redefs-fn {#'journal/cleanup! (fn [directory] (original directory) (throw (ex-info "cleanup detail" {})))}
             #(journal/journal-get s3 {}
                (fn [argv _ _ _]
                  (spit (nth argv 7) "{}")
                  {:exit 0 :out "{\"ETag\":\"etag\"}"})))))))

(deftest bound-secrets-in-inputs-never-reach-a-subprocess
  (let [r2-intent (assoc-in intent [:document :identity :backend]
                            {:kind "r2" :bucket "states" :region "auto" :endpoint "https://example.invalid"})
        environment {"COLORS_PAR_R2_ACCESS_KEY_ID" "r2-example-access" "COLORS_PAR_R2_SECRET_ACCESS_KEY" "r2-example-secret"}
        never-run (fn [& _] (throw (AssertionError. "secret input reached runner")))]
    (is (= {:status "error"}
           (journal/journal-put r2 (assoc-in r2-intent [:document :write_id] "r2-example-secret") environment never-run)))
    (is (= {:status "error"}
           (journal/journal-put r2 (assoc r2-intent :condition {:if_match "prefix-r2-example-secret-suffix"}) environment never-run)))
    (is (= {:status "error"}
           (journal/journal-get (assoc r2 :r2-endpoint "https://r2-example-secret.invalid") environment never-run)))
    (let [quoted "secret-with-\"quote\\slash"]
      (is (= {:status "error"}
             (journal/journal-put r2 (assoc r2-intent :condition {:if_match (str "prefix-" quoted "-suffix")})
               (assoc environment "COLORS_PAR_R2_SECRET_ACCESS_KEY" quoted) never-run))))))

(deftest utf8-bom-is-not-json
  (doseq [[body metadata] [["\uFEFF{}" "{\"ETag\":\"e\"}"] ["{}" "\uFEFF{\"ETag\":\"e\"}"]]]
    (is (= {:status "error"}
           (journal/journal-get s3 {}
             (fn [argv _ _ _]
               (spit (nth argv 7) body)
               {:exit 0 :out metadata}))))))
