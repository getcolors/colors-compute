(ns io.github.getcolors.compute-ssh
  "Deployment-owned local SSH access keys. Never returns private key material."
  (:require [clojure.java.io :as io]
            [clojure.string :as str]
            [io.github.getcolors.compute :as compute]
            [io.github.getcolors.compute-runtime :as runtime])
  (:import [java.nio.file Files Path LinkOption FileAlreadyExistsException]
           [java.nio.file.attribute PosixFilePermissions FileAttribute]
           [java.security MessageDigest]
           [java.util Base64]
           [java.nio ByteBuffer]
           [java.nio.charset StandardCharsets]))

(def placeholder-public "ssh-ed25519 PLACEHOLDER managed-by-colors")
(def ^:private nofollow (into-array LinkOption [LinkOption/NOFOLLOW_LINKS]))
(defn- refuse [message] (throw (ex-info message {:ssh/safe true})))
(defn- nonblank? [value]
  (and (string? value) (not (str/blank? value)) (not= "REPLACE_ME" (str/upper-case (str/trim value)))))
(defn- reference? [value]
  (or (nonblank? value)
      (and (vector? value) (seq value)
           (every? #(or (nonblank? %) (and (number? %) (< 0 % 9007199254740992) (== % (Math/floor (double %))))) value))))
(defn ^:no-doc mode [opts]
  (compute/state-keys (:profile opts) [])
  (let [provider (:provider-compute opts)
        entry (when (string? provider) (get-in compute/registry [:compute (keyword provider)]))]
    (when-not entry (refuse "invalid SSH compute provider"))
    (let [setting (:ssh-setting entry) key (keyword setting)
          identity-key (if (contains? opts :ssh-private-key-path) :ssh-private-key-path
                          (keyword (str provider "-ssh-private-key")))]
      (if (contains? opts key)
        (do
          (when-not (reference? (get opts key)) (refuse "invalid external SSH key reference"))
          (when (and (contains? opts identity-key) (not (nonblank? (get opts identity-key))))
            (refuse "invalid external SSH identity reference"))
          (cond-> {:mode "external" :setting setting :reference (get opts key)}
            (contains? opts identity-key) (assoc :private_key_path (get opts identity-key))))
        {:mode "managed"}))))
(defn- planning? [opts]
  (or (contains? #{:build "build"} (:green/event opts)) (true? (:green/dry-run opts))))
(defn- ownership! [ownership]
  (when-not
   (or (= {:status "fresh"} ownership)
       (and (map? ownership) (= #{:status :fingerprint} (set (keys ownership)))
            (= "prepared" (:status ownership))
            (string? (:fingerprint ownership))
            (re-matches #"SHA256:[A-Za-z0-9+/]{43}" (:fingerprint ownership))))
    (refuse "SSH key ownership uncertain")))
(defn- paths [opts environment]
  (let [home (if (nonblank? (get environment "HOME")) (get environment "HOME") (System/getProperty "user.home"))
        directory (.normalize (.toAbsolutePath (.toPath (io/file home ".ssh"))))
        private (.resolve directory (:profile opts))]
    {:directory directory :private private :public (.resolve directory (str (:profile opts) ".pub"))
     :known (.resolve directory (str (:profile opts) ".known_hosts"))}))
(defn- exists? [path] (Files/exists ^Path path nofollow))
(defn- safe-paths! [{:keys [directory private public known]}]
  (when (or (Files/isSymbolicLink directory)
            (and (exists? directory) (not (Files/isDirectory directory nofollow)))
            (some #(or (Files/isSymbolicLink %)
                       (and (exists? %) (not (Files/isRegularFile % nofollow)))) [private public known]))
    (refuse "unsafe SSH key path")))
(defn- permissions! [path mode]
  (Files/setPosixFilePermissions ^Path path (PosixFilePermissions/fromString mode)))
(defn- prepare-directory! [directory]
  (when-not (exists? directory)
    (try
      (Files/createDirectory directory
        (into-array FileAttribute [(PosixFilePermissions/asFileAttribute (PosixFilePermissions/fromString "rwx------"))]))
      (catch FileAlreadyExistsException _ nil)))
  (when-not (Files/isDirectory directory nofollow) (refuse "unsafe SSH key path"))
  (permissions! directory "rwx------"))
(defn- with-reservation! [{:keys [directory private] :as paths} operation]
  (prepare-directory! directory)
  (let [reservation (.resolve directory (str "." (.getFileName private) ".colors-key.lock"))]
    (try
      (Files/createFile reservation
        (into-array FileAttribute [(PosixFilePermissions/asFileAttribute (PosixFilePermissions/fromString "rw-------"))]))
      (catch FileAlreadyExistsException _ (refuse "SSH key reservation exists; explicit recovery required")))
    (let [identity (Files/readAttributes reservation "unix:dev,ino" nofollow)]
      (try
        (safe-paths! paths)
        (operation)
        (finally
          (when (and (exists? reservation) (= identity (Files/readAttributes reservation "unix:dev,ino" nofollow)))
            (Files/deleteIfExists reservation)))))))

(defn- public-parts [content]
  (let [content (str/trim content) parts (str/split content #"\s+")]
    (when (or (re-find #"[\r\n]" content) (< (count parts) 2) (not= "ssh-ed25519" (first parts)))
      (refuse "SSH keypair is inconsistent"))
    (take 2 parts)))
(defn- fingerprint [content]
  (let [[_ encoded] (public-parts content)
        blob (.decode (Base64/getDecoder) ^String encoded)
        buffer (ByteBuffer/wrap blob)]
    (when-not (and (= 51 (alength blob)) (= 11 (.getInt buffer)))
      (refuse "SSH keypair is inconsistent"))
    (let [algorithm (byte-array 11)]
      (.get buffer algorithm)
      (when-not (and (= "ssh-ed25519" (String. algorithm StandardCharsets/UTF_8)) (= 32 (.getInt buffer)) (= 32 (.remaining buffer)))
        (refuse "SSH keypair is inconsistent")))
    (str "SHA256:" (.encodeToString (.withoutPadding (Base64/getEncoder)) (.digest (MessageDigest/getInstance "SHA-256") blob)))))
(defn- derive-public [private directory environment runner]
  (let [result (runner ["ssh-keygen" "-y" "-P" "" "-f" (str private)] (str directory) environment 30000)]
    (when-not (and (= 0 (:exit result)) (string? (:out result))) (refuse "SSH keypair is inconsistent"))
    (str/trim (:out result))))
(defn- verify! [paths environment runner require-pair?]
  (let [{:keys [directory private public]} paths
        private? (exists? private) public? (exists? public)]
    (when (and require-pair? (not (and private? public?))) (refuse "owned SSH keypair is missing"))
    (permissions! directory "rwx------")
    (when private? (permissions! private "rw-------"))
    (when public? (permissions! public "rw-------"))
    (let [derived (when private? (derive-public private directory environment runner))
          published (when public? (str/trim (slurp (.toFile public))))]
      (when (and derived published (not= (public-parts derived) (public-parts published))) (refuse "SSH keypair is inconsistent"))
      {:public_key (or published derived) :fingerprint (fingerprint (or published derived))})))
(defn- acknowledge! [callback & args]
  (try
    (when-not (true? (apply callback args)) (refuse "SSH key ownership update failed"))
    (catch InterruptedException error (throw error))
    (catch Exception _ (refuse "SSH key ownership update failed"))))

(defn prepare-keypair*
  ([opts ownership record-intent record-prepared]
   (prepare-keypair* opts ownership (into {} (System/getenv)) record-intent record-prepared runtime/run-command))
  ([opts ownership environment record-intent record-prepared]
   (prepare-keypair* opts ownership environment record-intent record-prepared runtime/run-command))
  ([opts ownership environment record-intent record-prepared runner]
   (let [selected (mode opts)]
     (cond
       (= "external" (:mode selected)) selected
       (planning? opts) {:mode "managed" :private_key_path (str "$HOME/.ssh/" (:profile opts))
                         :public_key_path (str "$HOME/.ssh/" (:profile opts) ".pub")
                         :public_key placeholder-public :fingerprint nil}
       :else
       (do
         (when (and (contains? opts :green/event) (not (contains? #{:create "create"} (:green/event opts))))
           (refuse "SSH key preparation requires create"))
         (ownership! ownership)
         (let [{:keys [directory private public] :as paths} (paths opts environment)]
           (safe-paths! paths)
           (with-reservation! paths (fn []
           (if (= "fresh" (:status ownership))
             (do
               (when (or (exists? private) (exists? public) (exists? (:known paths))) (refuse "unowned SSH key files exist; verify surviving hosts before recovery"))
               (acknowledge! record-intent)
               (prepare-directory! directory)
               (let [result (runner ["ssh-keygen" "-q" "-t" "ed25519" "-N" "" "-C" (str (:profile opts) " managed by Colors")
                                     "-f" (str private)] (str directory) environment 30000)]
                 (when-not (= 0 (:exit result)) (refuse "SSH key generation failed")))
               (let [verified (verify! paths environment runner true)]
                 (acknowledge! record-prepared (:fingerprint verified))
                 (merge {:mode "managed" :private_key_path (str private) :public_key_path (str public)} verified)))
             (let [verified (verify! paths environment runner true)]
               (when-not (= (:fingerprint ownership) (:fingerprint verified)) (refuse "SSH key fingerprint differs from ownership"))
               (merge {:mode "managed" :private_key_path (str private) :public_key_path (str public)} verified))))))))
)))

(defn cleanup-keypair*
  ([opts ownership authority]
   (cleanup-keypair* opts ownership authority (into {} (System/getenv)) runtime/run-command))
  ([opts ownership authority environment]
   (cleanup-keypair* opts ownership authority environment runtime/run-command))
  ([opts ownership authority environment runner]
   (let [selected (mode opts)]
     (cond
       (= "external" (:mode selected)) selected
       (planning? opts) {:mode "managed" :cleaned false :planned true}
       :else
       (do
         (when (and (contains? opts :green/event) (not (contains? #{:delete "delete"} (:green/event opts))))
           (refuse "SSH key cleanup requires delete"))
         (when-not (and (map? authority) (every? #{:all_resources_destroyed :known_hosts_owned} (keys authority))
                        (true? (:all_resources_destroyed authority))
                        (or (not (contains? authority :known_hosts_owned)) (boolean? (:known_hosts_owned authority))))
           (refuse "SSH key cleanup requires complete resource destruction"))
         (ownership! ownership)
         (let [{:keys [directory private public known] :as paths} (paths opts environment)]
           (safe-paths! paths)
           (if-not (exists? directory)
             {:mode "managed" :cleaned true}
             (with-reservation! paths (fn []
           (when (or (exists? private) (exists? public))
             (when-not (= "prepared" (:status ownership)) (refuse "unowned SSH key files exist; verify surviving hosts before recovery"))
             (let [verified (verify! paths environment runner false)]
               (when-not (= (:fingerprint ownership) (:fingerprint verified)) (refuse "SSH key fingerprint differs from ownership")))
             (Files/deleteIfExists private)
             (Files/deleteIfExists public))
           (when (and (true? (:known_hosts_owned authority)) (exists? known))
             (when-not (= "prepared" (:status ownership)) (refuse "SSH known-host ownership uncertain"))
             (Files/deleteIfExists known))
           (when (exists? directory) (permissions! directory "rwx------"))
           {:mode "managed" :cleaned true})))))))))


(defn prepare-keypair!
  "Prepare or verify one deployment key; callbacks must confirm durable ownership."
  [& args]
  (try (apply prepare-keypair* args)
       (catch InterruptedException error (throw error))
       (catch Exception error
         (if (:ssh/safe (ex-data error)) (throw error) (refuse "SSH key operation failed")))))

(defn cleanup-keypair!
  "Remove owned local keys only after authoritative complete resource destruction."
  [& args]
  (try (apply cleanup-keypair* args)
       (catch InterruptedException error (throw error))
       (catch Exception error
         (if (:ssh/safe (ex-data error)) (throw error) (refuse "SSH key operation failed")))))
