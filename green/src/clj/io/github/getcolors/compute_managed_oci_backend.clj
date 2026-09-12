(ns io.github.getcolors.compute-managed-oci-backend
  (:require [clojure.string :as str] [io.github.getcolors.compute-oci :as oci]
            [io.github.getcolors.compute-coordinator :as coordinator]))
(def marker-key "_colors/backend-owner.json")
(defn- check [value message] (when-not value (throw (ex-info message {}))))
(defn lifecycle [opts action environment runner factory]
 (let [mode (get opts :oci-bucket-mode "external")]
  (check (contains? #{"managed" "external"} mode) "invalid OCI bucket mode")
  (if (or (= "external" mode) (some #(or (true? (get opts (keyword % "dry-run"))) (contains? #{:build "build"} (get opts (keyword % "event")))) ["blue" "red" "green"]))
   {:status "skipped"}
   (let [bucket (:oci-bucket opts) region (:oci-region opts) namespace (:oci-namespace opts) compartment (:oci-compartment-id opts) profile (:profile opts)
         _ (check (and (string? bucket) (re-matches #"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]" bucket)
                       (string? region) (re-matches #"[a-z][a-z0-9-]+" region)
                       (string? namespace) (re-matches #"[a-zA-Z0-9]+" namespace)
                       (string? compartment) (str/starts-with? compartment "ocid1.")
                       (string? profile) (re-matches #"[a-z0-9][a-z0-9_-]{0,62}" profile)) "invalid managed OCI identity")
         request (oci/client opts environment runner) path (oci/bucket-path opts) collection (subs path 0 (str/last-index-of path "/"))
         identity {:bucket bucket :region region :namespace namespace :compartment compartment :profile profile}
         tags {:colors-profile profile :colors-purpose "managed-backend"}
         present (request "GET" path nil {} {}) owner (atom nil) deleting (atom false)]
    (when-not present
     (loop [page nil]
      (let [listed (request "GET" collection nil (cond-> {:compartmentId compartment} page (assoc :page page)) {})]
       (check (and listed (vector? (:data listed)) (not-any? #(= bucket (:name %)) (:data listed))) "OCI bucket absence unconfirmed")
       (when-let [next-page (get-in listed [:headers :opc-next-page])] (recur next-page)))))
    (cond
     (= action "presence") {:status (if (nil? present) "absent" "present")}
     (and (nil? present) (or (= action "finalize") (some #(contains? #{:delete "delete"} (get opts (keyword % "event"))) ["blue" "red" "green"])))
     {:status "absent"}
     :else
     (try
      (let [observed (or present
                        (do (check (not (true? (:compute-require-existing-state opts))) "existing managed backend required")
                            (let [created (request "POST" collection {:name bucket :compartmentId compartment :freeformTags tags
                              :publicAccessType "NoPublicAccess" :versioning "Enabled"} {} {})]
                             (check (and created (not (:conflict created))) "managed backend creation conflict")
                             (let [written (request "PUT" (oci/object-path opts marker-key) {:schema 1 :identity identity :status "active"} {} {:if-none-match "*" :content-type "application/json"})]
                              (check (and written (not (:conflict written))) "managed backend ownership conflict")) created)))
            metadata (:data observed)
            _ (check (and (= compartment (:compartmentId metadata)) (= tags (select-keys (:freeformTags metadata) (keys tags)))) "managed backend ownership mismatch")
            marker-response (request "GET" (oci/object-path opts marker-key) nil {} {})
            marker (or (:data marker-response) (when (and (= action "finalize") (= "deleting" (get-in metadata [:freeformTags :colors-phase]))) {:schema 1 :identity identity :status "deleting"}))
            _ (check (and (= 1 (:schema marker)) (= identity (:identity marker)) (contains? #{"active" "deleting"} (:status marker))) "managed backend ownership mismatch")
            marker (if (and (= action "finalize") (= "deleting" (get-in metadata [:freeformTags :colors-phase]))) (assoc marker :status "deleting") marker)
            update! (fn [body] (let [result (request "POST" path body {} {:content-type "application/json" :if-match (get-in observed [:headers :etag])})] (check (and result (not (:conflict result))) "managed backend metadata conflict")))
            list-objects (fn [versions] (loop [start nil items []]
              (let [response (request "GET" (str path (if versions "/objectversions" "/o")) nil (cond-> {} start (assoc (if versions :page :start) start)) {})
                    _ (check (and response (not (:conflict response))) "managed backend listing failed")
                    page (:data response) entries (get page (if versions :items :objects))
                    _ (check (vector? entries) "managed backend listing malformed") items (into items entries)
                    next-page (if versions (get-in response [:headers :opc-next-page]) (:nextStartWith page))]
               (if next-page (recur next-page items) items))))]
       (if (= action "bootstrap")
        (do (check (and (= "active" (:status marker)) (not= "deleting" (get-in metadata [:freeformTags :colors-phase]))) "managed backend deletion in progress")
            (when (or (not= "Enabled" (:versioning metadata)) (not= "NoPublicAccess" (:publicAccessType metadata)))
              (update! {:publicAccessType "NoPublicAccess" :versioning "Enabled"}))
            {:status "ready" :bucket bucket})
        (do
         (check (false? (:compute-prevent-destroy opts)) "managed backend deletion protected")
         (when (= "active" (:status marker))
          (reset! owner (factory opts environment)) (coordinator/acquire! @owner)
          (check (= "retired" (get-in (coordinator/snapshot @owner) [:document :status])) "compute must retire before backend deletion")
          (doseq [item (list-objects false) :let [key (:name item)] :when (not (contains? #{marker-key (str profile "/compute/coordination.json")} key))]
           (check (and (str/starts-with? key (str profile "/")) (str/ends-with? key ".tfstate")) "managed backend contains unexpected objects")
           (let [state (:data (request "GET" (oci/object-path opts key) nil {} {}))]
            (check (and (= 4 (:version state)) (vector? (:resources state)) (not-any? #(seq (:instances %)) (:resources state))) "managed backend contains live state")))
          (let [written (request "PUT" (oci/object-path opts marker-key) (assoc marker :status "deleting") {} {:if-match (get-in marker-response [:headers :etag]) :content-type "application/json"})]
           (check (and written (not (:conflict written))) "managed backend ownership conflict"))
          (reset! deleting true))
         (update! {:freeformTags (assoc (:freeformTags metadata) :colors-phase "deleting")})
         (doseq [item (sort-by #(= marker-key (:name %)) (list-objects true))]
          (check (seq (:versionId item)) "managed backend version missing")
          (check (not (:conflict (request "DELETE" (oci/object-path opts (:name item)) nil {:versionId (:versionId item)} {}))) "managed backend version conflict"))
         (check (not (:conflict (request "DELETE" path nil {} {}))) "managed backend bucket deletion conflict")
         {:status "destroyed"})))
      (finally (when (and @owner (not @deleting)) (coordinator/release! @owner)))))))))
