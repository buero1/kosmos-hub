<?php
namespace KosmosBridge\Abilities;

defined( 'ABSPATH' ) || exit;

class Registry {
	const UPDATE_LOOPBACK_ACTION = 'kosmos_bridge_collect_update_inventory';
	const UPDATE_LOOPBACK_TTL    = 60;
	const UPDATE_OFFER_CACHE_OPTION = 'kosmos_bridge_plugin_update_offers_v1';
	const UPDATE_OFFER_CACHE_TTL    = 172800;
	const UPDRAFT_BACKUP_REQUEST_ACTION    = 'updraft_backupnow_backup_all';
	const UPDRAFT_BACKUP_LOOPBACK_ACTION   = 'kosmos_bridge_start_updraftplus_backup';
	const UPDRAFT_BACKUP_REQUEST_OPTION    = 'kosmos_bridge_updraft_backup_request';
	const UPDRAFT_BACKUP_REQUEST_TTL       = 300;
	const UPDRAFT_BACKUP_PENDING_MAX_AGE   = 180;

	/**
	 * @return void
	 */
	public static function register_categories() {
		if ( ! function_exists( 'wp_register_ability_category' ) ) {
			return;
		}

		wp_register_ability_category(
			'kosmos-bridge',
			array(
				'label'       => __( 'Kosmos Bridge', 'kosmos-bridge' ),
				'description' => __( 'Controlled site abilities exposed to the authenticated Kosmos Hub.', 'kosmos-bridge' ),
			)
		);
	}

	/**
	 * @return void
	 */
	public static function register_abilities() {
		if ( ! function_exists( 'wp_register_ability' ) ) {
			return;
		}

		wp_register_ability(
			'kosmos-bridge/get-site-info',
			array(
				'label'               => __( 'Get Site Info', 'kosmos-bridge' ),
				'description'         => __( 'Returns basic information about this WordPress site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::site_info_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_site_info' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/get-environment-info',
			array(
				'label'               => __( 'Get Environment Info', 'kosmos-bridge' ),
				'description'         => __( 'Returns software versions and runtime information for this WordPress site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::environment_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_environment_info' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/list-active-plugins',
			array(
				'label'               => __( 'List Active Plugins', 'kosmos-bridge' ),
				'description'         => __( 'Returns active plugin metadata for this WordPress site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::active_plugins_output_schema(),
				'execute_callback'    => array( self::class, 'execute_list_active_plugins' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/get-available-updates',
			array(
				'label'               => __( 'Get Available Updates', 'kosmos-bridge' ),
				'description'         => __( 'Checks and returns currently available WordPress, plugin, and theme updates for this site.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::available_updates_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_available_updates' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/get-updraftplus-backup-status',
			array(
				'label'               => __( 'Get UpdraftPlus Backup Status', 'kosmos-bridge' ),
				'description'         => __( 'Returns read-only metadata about the latest complete UpdraftPlus backup or one requested backup.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::updraftplus_backup_status_input_schema(),
				'output_schema'       => self::updraftplus_backup_status_output_schema(),
				'execute_callback'    => array( self::class, 'execute_get_updraftplus_backup_status' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/start-updraftplus-backup',
			array(
				'label'               => __( 'Start UpdraftPlus Backup', 'kosmos-bridge' ),
				'description'         => __( 'Starts one full UpdraftPlus backup using the site configuration and protects it from automatic deletion. It cannot download, restore, or change backup settings.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::updraftplus_backup_start_input_schema(),
				'output_schema'       => self::updraftplus_backup_start_output_schema(),
				'execute_callback'    => array( self::class, 'execute_start_updraftplus_backup' ),
				'permission_callback' => array( self::class, 'allow_mutation_access' ),
				'meta'                => self::mutation_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/list-updraftplus-backups',
			array(
				'label'               => __( 'List UpdraftPlus Backups', 'kosmos-bridge' ),
				'description'         => __( 'Returns read-only metadata for the UpdraftPlus backup sets known to this site. It does not expose backup files or credentials.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::updraftplus_backup_start_input_schema(),
				'output_schema'       => self::updraftplus_backup_list_output_schema(),
				'execute_callback'    => array( self::class, 'execute_list_updraftplus_backups' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/delete-updraftplus-backup',
			array(
				'label'               => __( 'Delete UpdraftPlus Backup', 'kosmos-bridge' ),
				'description'         => __( 'Deletes one exact complete or partial UpdraftPlus backup set locally and from its configured remote storage. The Hub must provide the exact nonce and timestamp.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::updraftplus_backup_delete_input_schema(),
				'output_schema'       => self::updraftplus_backup_delete_output_schema(),
				'execute_callback'    => array( self::class, 'execute_delete_updraftplus_backup' ),
				'permission_callback' => array( self::class, 'allow_mutation_access' ),
				'meta'                => self::destructive_mutation_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/verify-updraftplus-backup-deletion',
			array(
				'label'               => __( 'Verify UpdraftPlus Backup Deletion', 'kosmos-bridge' ),
				'description'         => __( 'Rescans configured remote storage and confirms whether one exact UpdraftPlus backup set is gone. It does not delete backup files.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::updraftplus_backup_deletion_verification_input_schema(),
				'output_schema'       => self::updraftplus_backup_deletion_verification_output_schema(),
				'execute_callback'    => array( self::class, 'execute_verify_updraftplus_backup_deletion' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/check-site-health',
			array(
				'label'               => __( 'Check Site Health', 'kosmos-bridge' ),
				'description'         => __( 'Performs read-only public homepage and WordPress REST API health checks.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'output_schema'       => self::site_health_output_schema(),
				'execute_callback'    => array( self::class, 'execute_check_site_health' ),
				'permission_callback' => array( self::class, 'allow_readonly_access' ),
				'meta'                => self::readonly_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/update-plugin',
			array(
				'label'               => __( 'Update Plugin', 'kosmos-bridge' ),
				'description'         => __( 'Updates one active plugin after an exact version and active-state preflight.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::plugin_update_input_schema(),
				'output_schema'       => self::plugin_update_output_schema(),
				'execute_callback'    => array( self::class, 'execute_update_plugin' ),
				'permission_callback' => array( self::class, 'allow_mutation_access' ),
				'meta'                => self::mutation_meta(),
			)
		);

		wp_register_ability(
			'kosmos-bridge/activate-plugin',
			array(
				'label'               => __( 'Activate Plugin', 'kosmos-bridge' ),
				'description'         => __( 'Reactivates one plugin only when its installed version matches the Hub recovery plan.', 'kosmos-bridge' ),
				'category'            => 'kosmos-bridge',
				'input_schema'        => self::plugin_activation_input_schema(),
				'output_schema'       => self::plugin_activation_output_schema(),
				'execute_callback'    => array( self::class, 'execute_activate_plugin' ),
				'permission_callback' => array( self::class, 'allow_mutation_access' ),
				'meta'                => self::mutation_meta(),
			)
		);

	}

	/**
	 * Handle the signed local loopback outside the Hub HTTP request. The token is
	 * single-use and only the matching queued backup request may start work.
	 *
	 * @return void
	 */
	public static function handle_background_updraftplus_backup() {
		$nonce = isset( $_POST['backup_nonce'] ) ? (string) wp_unslash( $_POST['backup_nonce'] ) : '';
		$token = isset( $_POST['token'] ) ? (string) wp_unslash( $_POST['token'] ) : '';
		if ( ! self::consume_updraftplus_backup_loopback_token( $nonce, $token ) ) {
			wp_send_json_error( array( 'code' => 'kosmos_bridge_loopback_forbidden' ), 403 );
		}

		ignore_user_abort( true );
		self::run_background_updraftplus_backup( $nonce );
		wp_send_json_success();
	}

	/**
	 * Run the protected UpdraftPlus start after the signed loopback is accepted.
	 *
	 * @param string $nonce UpdraftPlus backup nonce prepared by the Bridge.
	 * @return void
	 */
	private static function run_background_updraftplus_backup( $nonce ) {
		if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) ) {
			return;
		}

		$pending = self::get_updraftplus_backup_request( $nonce );
		if ( empty( $pending ) || 'queued' !== ( $pending['status'] ?? '' ) ) {
			return;
		}

		self::update_updraftplus_backup_request(
			$nonce,
			array(
				'status'  => 'starting',
				'message' => 'The WordPress background worker is starting the protected backup with UpdraftPlus.',
			)
		);

		if ( ! isset( $GLOBALS['updraftplus'] ) || ! is_object( $GLOBALS['updraftplus'] ) ) {
			self::update_updraftplus_backup_request(
				$nonce,
				array(
					'status'  => 'failed',
					'message' => 'The WordPress background worker could not initialize UpdraftPlus.',
				)
			);
			return;
		}

		try {
			do_action(
				self::UPDRAFT_BACKUP_REQUEST_ACTION,
				array(
					'use_nonce'   => $nonce,
					'always_keep' => true,
				)
			);
		} catch ( \Throwable $error ) {
			self::update_updraftplus_backup_request(
				$nonce,
				array(
					'status'  => 'failed',
					'message' => 'UpdraftPlus backup could not be started: ' . $error->getMessage(),
				)
			);
			return;
		}

		self::update_updraftplus_backup_request(
			$nonce,
			array(
				'status'  => 'running',
				'message' => 'UpdraftPlus accepted the protected backup and is writing it to the configured destination.',
			)
		);
	}

	/**
	 * @return bool
	 */
	public static function allow_readonly_access() {
		return true;
	}

	/**
	 * The MCP REST endpoint authenticates every request with the per-site HMAC
	 * signature before WordPress evaluates an ability permission callback.
	 *
	 * @return bool
	 */
	public static function allow_mutation_access() {
		return true;
	}

	/**
	 * @return array
	 */
	public static function execute_get_site_info() {
		return array(
			'name'         => get_bloginfo( 'name' ),
			'description'  => get_bloginfo( 'description' ),
			'home_url'     => home_url( '/' ),
			'site_url'     => site_url( '/' ),
			'language'     => get_bloginfo( 'language' ),
			'timezone'     => wp_timezone_string(),
			'is_multisite' => is_multisite(),
		);
	}

	/**
	 * @return array
	 */
	public static function execute_get_environment_info() {
		global $wp_version;

		return array(
			'wordpress_version' => is_string( $wp_version ) ? $wp_version : get_bloginfo( 'version' ),
			'php_version'       => PHP_VERSION,
			'bridge_version'    => \KosmosBridge\Options::get_bridge_version(),
			'abilities_api'     => function_exists( 'wp_register_ability' ),
			'site_uuid'         => \KosmosBridge\Options::get_site_uuid(),
		);
	}

	/**
	 * @return array
	 */
	public static function execute_list_active_plugins() {
		if ( ! function_exists( 'get_plugins' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}

		$plugins        = get_plugins();
		$active_plugins = array_values( (array) get_option( 'active_plugins', array() ) );
		$items          = array();

		foreach ( $active_plugins as $plugin_file ) {
			$data    = isset( $plugins[ $plugin_file ] ) ? $plugins[ $plugin_file ] : array();
			$items[] = array(
				'plugin_file' => $plugin_file,
				'name'        => isset( $data['Name'] ) ? (string) $data['Name'] : $plugin_file,
				'version'     => isset( $data['Version'] ) ? (string) $data['Version'] : '',
				'author'      => isset( $data['AuthorName'] ) ? (string) $data['AuthorName'] : '',
				'plugin_uri'  => isset( $data['PluginURI'] ) ? (string) $data['PluginURI'] : '',
			);
		}

		return array(
			'count'   => count( $items ),
			'plugins' => $items,
		);
	}

	/**
	 * Read UpdraftPlus backup metadata without starting, downloading, restoring,
	 * or exposing backup files. The provider resolves a full backup against the
	 * backup entities configured on the individual site.
	 *
	 * @param array|null $input Optional exact UpdraftPlus backup nonce.
	 * @return array
	 */
	public static function execute_get_updraftplus_backup_status( $input = null ) {
		$plugin_file = 'updraftplus/updraftplus.php';
		$installed   = file_exists( WP_PLUGIN_DIR . '/' . $plugin_file );
		$active      = in_array( $plugin_file, (array) get_option( 'active_plugins', array() ), true );
		$requested_nonce = self::get_requested_updraftplus_backup_nonce( $input );

		if ( is_multisite() ) {
			$network_active = (array) get_site_option( 'active_sitewide_plugins', array() );
			$active         = $active || isset( $network_active[ $plugin_file ] );
		}

		$result = array(
			'reported_at'         => gmdate( 'c' ),
			'provider'            => 'updraftplus',
			'installed'           => $installed,
			'active'              => $active,
			'available'           => false,
			'complete'            => false,
			'retention_protected' => false,
			'latest_backup_at'    => '',
			'backup_nonce'        => '',
			'backup_count'        => 0,
			'components'          => array(),
			'request_status'      => 'not_requested',
			'request_updated_at'  => '',
			'request_message'     => '',
			'message'             => '',
		);
		$pending = self::get_updraftplus_backup_request( $requested_nonce );
		if ( ! empty( $pending ) ) {
			$result['request_status']     = isset( $pending['status'] ) ? (string) $pending['status'] : 'queued';
			$result['request_updated_at'] = ! empty( $pending['updated_at'] ) ? gmdate( 'c', (int) $pending['updated_at'] ) : '';
			$result['request_message']    = isset( $pending['message'] ) ? (string) $pending['message'] : '';
		}

		if ( ! $installed ) {
			$result['message'] = 'UpdraftPlus is not installed on this site.';
			return $result;
		}

		if ( ! $active || ! class_exists( 'UpdraftPlus_Backup_History', false ) ) {
			$result['message'] = 'UpdraftPlus is installed but not active or not initialized.';
			return $result;
		}

		try {
			$history = \UpdraftPlus_Backup_History::get_history();
			$history = is_array( $history ) ? $history : array();
			$nonce   = '' !== $requested_nonce ? $requested_nonce : (string) \UpdraftPlus_Backup_History::get_latest_full_backup();
			$backup  = self::find_updraftplus_backup_by_nonce( $history, $nonce );
		} catch ( \Throwable $exception ) {
			$result['message'] = 'UpdraftPlus backup history could not be read.';
			return $result;
		}

		$result['backup_count'] = count( $history );
		if ( empty( $backup ) ) {
			$result['message'] = ! empty( $result['request_message'] )
				? $result['request_message']
				: 'No complete UpdraftPlus backup is currently recorded.';
			return $result;
		}

		$timestamp = self::get_updraftplus_backup_timestamp( $backup );
		if ( $timestamp > 0 ) {
			$result['latest_backup_at'] = gmdate( 'c', $timestamp );
		}

		$result['available']            = $timestamp > 0;
		$result['complete']             = true;
		$result['retention_protected']  = ! empty( $backup['always_keep'] );
		$result['backup_nonce']         = $nonce;
		$result['components']           = self::get_updraftplus_backup_components( $backup );
		$result['message']              = $result['available']
			? ( $result['retention_protected'] ? 'Complete UpdraftPlus backup found and protected from automatic deletion.' : 'Complete UpdraftPlus backup found but is not protected from automatic deletion.' )
			: 'A complete backup was found without a usable timestamp.';

		if ( '' !== $requested_nonce && $requested_nonce === $nonce ) {
			$result['request_status']     = 'completed';
			$result['request_updated_at'] = gmdate( 'c' );
			$result['request_message']    = 'UpdraftPlus recorded the requested protected backup.';
			self::clear_updraftplus_backup_request( $nonce );
		}

		return $result;
	}

	/**
	 * List known UpdraftPlus backup sets without exposing archive locations or
	 * storage credentials. The Hub uses the exact nonce and timestamp to scope
	 * a later deletion to one previously observed backup set.
	 *
	 * @return array
	 */
	public static function execute_list_updraftplus_backups( $input = null ) {
		$availability = self::get_updraftplus_availability();
		$result       = array(
			'provider'  => 'updraftplus',
			'installed' => $availability['installed'],
			'active'    => $availability['active'],
			'backups'   => array(),
			'message'   => '',
		);

		if ( ! $availability['installed'] || ! $availability['active'] || ! class_exists( 'UpdraftPlus_Backup_History', false ) ) {
			$result['message'] = 'UpdraftPlus is not installed, active, or initialized on this site.';
			return $result;
		}

		try {
			$history = \UpdraftPlus_Backup_History::get_history();
			$history = is_array( $history ) ? $history : array();
		} catch ( \Throwable $exception ) {
			$result['message'] = 'UpdraftPlus backup history could not be read.';
			return $result;
		}

		foreach ( $history as $backup_time => $backup ) {
			if ( ! is_array( $backup ) || ! isset( $backup['nonce'] ) ) {
				continue;
			}

			$nonce     = (string) $backup['nonce'];
			$timestamp = self::get_updraftplus_history_timestamp( $backup_time, $backup );
			if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) || $timestamp <= 0 ) {
				continue;
			}

			$result['backups'][] = array(
				'backup_nonce'        => $nonce,
				'backup_timestamp'    => $timestamp,
				'backup_at'           => gmdate( 'c', $timestamp ),
				'complete'            => self::is_complete_updraftplus_backup( $backup ),
				'retention_protected' => ! empty( $backup['always_keep'] ),
				'components'          => self::get_updraftplus_backup_components( $backup ),
			);
		}

		usort(
			$result['backups'],
			static function ( $left, $right ) {
				return $left['backup_timestamp'] <=> $right['backup_timestamp'];
			}
		);
		$result['message'] = empty( $result['backups'] )
			? 'No UpdraftPlus backup sets are currently recorded.'
			: 'UpdraftPlus backup metadata was read successfully.';

		return $result;
	}

	/**
	 * Delete exactly one known UpdraftPlus backup set through the provider's own
	 * deletion API. Remote deletion is mandatory so local and remote retention
	 * cannot drift apart. The provider receives one uninterrupted deletion call
	 * so its own backup-set state is never resumed mid-component.
	 *
	 * @param mixed $input Exact backup identity and deletion policy.
	 * @return array|\WP_Error
	 */
	public static function execute_delete_updraftplus_backup( $input = array() ) {
		$nonce                  = self::get_input_string( $input, 'backup_nonce' );
		$timestamp              = self::get_updraftplus_backup_timestamp_input( $input );
		$allow_protected_delete = true === self::get_input_bool( $input, 'allow_protected_delete' );

		if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) || $timestamp <= 0 || ! self::get_input_bool( $input, 'delete_remote' ) ) {
			return new \WP_Error(
				'kosmos_bridge_invalid_updraftplus_backup_delete_input',
				'Backup nonce, backup timestamp, and delete_remote=true are required.',
				array( 'status' => 400 )
			);
		}

		$availability = self::get_updraftplus_availability();
		if ( ! $availability['installed'] || ! $availability['active'] || ! class_exists( 'UpdraftPlus_Backup_History', false ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_unavailable',
				'UpdraftPlus must be installed, active, and initialized before a backup can be deleted.',
				array( 'status' => 409 )
			);
		}

		try {
			$history = \UpdraftPlus_Backup_History::get_history();
			$history = is_array( $history ) ? $history : array();
			$backup  = self::find_updraftplus_backup_by_nonce( $history, $nonce );
		} catch ( \Throwable $exception ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_history_unavailable',
				'UpdraftPlus backup history could not be read before deletion.',
				array( 'status' => 502 )
			);
		}

		if ( empty( $backup ) || self::get_updraftplus_backup_timestamp( $backup ) !== $timestamp ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_backup_not_found',
				'The requested UpdraftPlus backup no longer matches the observed backup set.',
				array( 'status' => 409 )
			);
		}

		if ( ! $allow_protected_delete && ! empty( $backup['always_keep'] ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_backup_protected',
				'The requested backup is protected for manual deletion only and cannot be removed by automatic cleanup.',
				array( 'status' => 409 )
			);
		}

		$updraftplus_admin = self::get_updraftplus_admin();
		if ( ! is_object( $updraftplus_admin ) || ! method_exists( $updraftplus_admin, 'delete_set' ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_delete_unavailable',
				'UpdraftPlus could not initialize its backup deletion service.',
				array( 'status' => 502 )
			);
		}

		try {
			$deletion = $updraftplus_admin->delete_set(
				array(
					'backup_timestamp' => (string) $timestamp,
					'delete_remote'    => true,
				)
			);
		} catch ( \Throwable $exception ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_delete_failed',
				'UpdraftPlus could not delete the requested backup: ' . $exception->getMessage(),
				array( 'status' => 502 )
			);
		}

		return self::updraftplus_backup_delete_result( $nonce, $timestamp, $deletion );
	}

	/**
	 * Confirm a deletion by rebuilding UpdraftPlus' backup history from the
	 * configured remote storage. A provider acknowledgement alone is not proof
	 * that every remote archive from a set was removed.
	 *
	 * @param mixed $input Exact backup identity.
	 * @return array|\WP_Error
	 */
	public static function execute_verify_updraftplus_backup_deletion( $input = array() ) {
		$nonce     = self::get_input_string( $input, 'backup_nonce' );
		$timestamp = self::get_updraftplus_backup_timestamp_input( $input );

		if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) || $timestamp <= 0 ) {
			return new \WP_Error(
				'kosmos_bridge_invalid_updraftplus_backup_deletion_verification_input',
				'Backup nonce and backup timestamp are required for deletion verification.',
				array( 'status' => 400 )
			);
		}

		$availability = self::get_updraftplus_availability();
		if ( ! $availability['installed'] || ! $availability['active'] || ! class_exists( 'UpdraftPlus_Backup_History', false ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_unavailable',
				'UpdraftPlus must be installed, active, and initialized before backup deletion can be verified.',
				array( 'status' => 409 )
			);
		}

		try {
			$diagnostics = \UpdraftPlus_Backup_History::rebuild( true );
			$history     = \UpdraftPlus_Backup_History::get_history();
			$history     = is_array( $history ) ? $history : array();
			$remaining   = self::find_updraftplus_backup_by_nonce( $history, $nonce );
		} catch ( \Throwable $exception ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_remote_rescan_failed',
				'UpdraftPlus could not rescan remote storage after the backup deletion: ' . $exception->getMessage(),
				array( 'status' => 502 )
			);
		}

		if ( ! empty( $diagnostics ) ) {
			return array(
				'backup_nonce'         => $nonce,
				'backup_timestamp'     => $timestamp,
				'verified'             => false,
				'remaining_components' => empty( $remaining ) ? array() : self::get_updraftplus_backup_components( $remaining ),
				'message'              => 'UpdraftPlus remote rescan returned diagnostics, so complete backup deletion cannot be verified.',
			);
		}

		if ( empty( $remaining ) ) {
			return array(
				'backup_nonce'         => $nonce,
				'backup_timestamp'     => $timestamp,
				'verified'             => true,
				'remaining_components' => array(),
				'message'              => 'UpdraftPlus remote rescan confirmed that the requested backup set is no longer present.',
			);
		}

		return array(
			'backup_nonce'         => $nonce,
			'backup_timestamp'     => $timestamp,
			'verified'             => false,
			'remaining_components' => self::get_updraftplus_backup_components( $remaining ),
			'message'              => 'UpdraftPlus remote rescan still found files from the requested backup set.',
		);
	}

	/**
	 * Queue exactly one full UpdraftPlus backup and return its generated nonce.
	 * A signed non-blocking local loopback runs the same action in the background,
	 * keeping the Hub request responsive without relying on WordPress Cron.
	 *
	 * @param array|null $input Empty ability input.
	 * @return array|\WP_Error
	 */
	public static function execute_start_updraftplus_backup( $input = null ) {
		$plugin_file = 'updraftplus/updraftplus.php';
		$installed   = file_exists( WP_PLUGIN_DIR . '/' . $plugin_file );
		$active      = in_array( $plugin_file, (array) get_option( 'active_plugins', array() ), true );

		if ( is_multisite() ) {
			$network_active = (array) get_site_option( 'active_sitewide_plugins', array() );
			$active         = $active || isset( $network_active[ $plugin_file ] );
		}

		if ( ! $installed || ! $active || ! isset( $GLOBALS['updraftplus'] ) || ! is_object( $GLOBALS['updraftplus'] ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_unavailable',
				'UpdraftPlus must be installed, active, and initialized before a backup can start.',
				array( 'status' => 409 )
			);
		}

		$pending = self::get_current_updraftplus_backup_request();
		if ( is_array( $pending ) && ! empty( $pending['nonce'] ) && self::is_valid_updraftplus_backup_nonce( $pending['nonce'] ) ) {
			$requested_at = isset( $pending['requested_at'] ) ? (int) $pending['requested_at'] : 0;
			if ( $requested_at > time() - self::UPDRAFT_BACKUP_PENDING_MAX_AGE ) {
				return new \WP_Error(
					'kosmos_bridge_updraftplus_backup_pending',
					'An UpdraftPlus backup requested by Kosmos Hub is already pending.',
					array( 'status' => 409 )
				);
			}

			delete_option( self::UPDRAFT_BACKUP_REQUEST_OPTION );
		}

		$updraftplus = $GLOBALS['updraftplus'];
		if ( ! method_exists( $updraftplus, 'backup_time_nonce' ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_unavailable',
				'UpdraftPlus could not prepare a backup identifier.',
				array( 'status' => 409 )
			);
		}

		$nonce = (string) $updraftplus->backup_time_nonce();
		if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) ) {
			return new \WP_Error(
				'kosmos_bridge_updraftplus_nonce_failed',
				'UpdraftPlus did not return a usable backup identifier.',
				array( 'status' => 500 )
			);
		}

		$token = self::generate_loopback_token();
		self::store_updraftplus_backup_request(
			array(
				'nonce'               => $nonce,
				'requested_at'        => time(),
				'updated_at'          => time(),
				'status'              => 'queued',
				'message'             => 'The Bridge queued the protected backup for its background worker.',
				'dispatch_token_hash' => hash( 'sha256', $token ),
			)
		);

		$response = wp_remote_post(
			admin_url( 'admin-ajax.php' ),
			array(
				'timeout'     => 1,
				'redirection' => 0,
				'blocking'    => false,
				'sslverify'   => true,
				'body'        => array(
					'action'       => self::UPDRAFT_BACKUP_LOOPBACK_ACTION,
					'backup_nonce' => $nonce,
					'token'        => $token,
				),
			)
		);
		if ( is_wp_error( $response ) ) {
			delete_option( self::UPDRAFT_BACKUP_REQUEST_OPTION );

			return new \WP_Error(
				'kosmos_bridge_updraftplus_dispatch_failed',
				'WordPress could not dispatch the UpdraftPlus backup background task.',
				array( 'status' => 502 )
			);
		}

		return array(
			'accepted'                       => true,
			'provider'                       => 'updraftplus',
			'backup_nonce'                   => $nonce,
			'retention_protection_requested' => true,
			'request_status'                 => 'queued',
			'background_dispatch_requested'  => true,
			'scheduled_at'                   => gmdate( 'c' ),
			'message'                        => 'The protected UpdraftPlus backup was queued for immediate background processing.',
		);
	}

	/**
	 * Verify that the public homepage and the WordPress REST API still return a
	 * successful HTTP response. This does not change site content or settings.
	 *
	 * @return array
	 */
	public static function execute_check_site_health() {
		$home_url = home_url( '/' );
		$rest_url = rest_url( '/' );
		$home     = self::request_health_url( $home_url );
		$rest     = self::request_health_url( $rest_url );

		return array(
			'home_url'      => $home_url,
			'rest_url'      => $rest_url,
			'home_status'   => $home['status'],
			'rest_status'   => $rest['status'],
			'home_healthy'  => $home['healthy'],
			'rest_healthy'  => $rest['healthy'],
			'message'       => self::site_health_message( $home, $rest ),
		);
	}

	/**
	 * Update exactly one approved active WordPress plugin. The Hub must provide
	 * the plugin file and exact current and target versions from a single-item
	 * plan. Package URLs and version ranges are never accepted from the Hub.
	 *
	 * @param mixed $input Plugin file and expected versions from the Hub plan.
	 * @return array|\WP_Error
	 */
	public static function execute_update_plugin( $input = array() ) {
		$plugin_file     = self::get_input_string( $input, 'plugin_file' );
		$expected_current = self::get_input_string( $input, 'expected_current_version' );
		$expected_target  = self::get_input_string( $input, 'expected_target_version' );

		if ( ! self::is_valid_plugin_file( $plugin_file ) || '' === $expected_current || '' === $expected_target ) {
			return new \WP_Error(
				'kosmos_bridge_invalid_update_input',
				'Plugin file and expected current and target versions are required.',
				array( 'status' => 400 )
			);
		}

		if ( ! function_exists( 'get_plugins' ) || ! function_exists( 'is_plugin_active' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}

		$plugins = get_plugins();
		if ( ! isset( $plugins[ $plugin_file ] ) ) {
			return new \WP_Error(
				'kosmos_bridge_plugin_not_installed',
				'The approved plugin is not installed on this site.',
				array( 'status' => 409 )
			);
		}

		$current_version = isset( $plugins[ $plugin_file ]['Version'] ) ? (string) $plugins[ $plugin_file ]['Version'] : '';
		if ( $current_version !== $expected_current ) {
			return new \WP_Error(
				'kosmos_bridge_update_version_mismatch',
				sprintf( 'The approved plugin is at version %s, not the approved version %s.', $current_version, $expected_current ),
				array( 'status' => 409 )
			);
		}

		if ( ! is_plugin_active( $plugin_file ) ) {
			return new \WP_Error(
				'kosmos_bridge_plugin_inactive',
				'The approved plugin must be active before the Hub can update it.',
				array( 'status' => 409 )
			);
		}

		self::refresh_update_transients( $plugins );
		$plugin_transient = self::to_array( get_site_transient( 'update_plugins' ) );
		$responses        = isset( $plugin_transient['response'] ) ? self::to_array( $plugin_transient['response'] ) : array();
		$offer            = isset( $responses[ $plugin_file ] ) ? self::to_array( $responses[ $plugin_file ] ) : array();
		$offered_version  = self::get_update_version( $offer );
		$package          = isset( $offer['package'] ) ? trim( (string) $offer['package'] ) : '';

		if ( $offered_version !== $expected_target || '' === $package ) {
			return new \WP_Error(
				'kosmos_bridge_update_offer_changed',
				'The approved plugin update offer is no longer available.',
				array( 'status' => 409 )
			);
		}

		self::load_plugin_upgrader();
		if ( ! class_exists( '\Plugin_Upgrader' ) || ! class_exists( '\Automatic_Upgrader_Skin' ) ) {
			return new \WP_Error(
				'kosmos_bridge_upgrader_unavailable',
				'WordPress could not load its plugin updater.',
				array( 'status' => 500 )
			);
		}

		$upgrader = new \Plugin_Upgrader( new \Automatic_Upgrader_Skin() );
		$result   = $upgrader->upgrade( $plugin_file, array( 'clear_update_cache' => true ) );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		if ( true !== $result ) {
			return new \WP_Error(
				'kosmos_bridge_update_failed',
				'WordPress did not confirm the plugin update.',
				array( 'status' => 500 )
			);
		}

		if ( function_exists( 'wp_clean_plugins_cache' ) ) {
			wp_clean_plugins_cache( true );
		}
		$plugins         = get_plugins();
		$installed_after = isset( $plugins[ $plugin_file ]['Version'] ) ? (string) $plugins[ $plugin_file ]['Version'] : '';
		if ( $installed_after !== $expected_target ) {
			return new \WP_Error(
				'kosmos_bridge_update_verification_failed',
				'WordPress completed the update but the installed plugin version could not be verified.',
				array( 'status' => 500 )
			);
		}

		$activation = self::activate_plugin_and_verify( $plugin_file );
		if ( is_wp_error( $activation ) ) {
			return $activation;
		}

		return array(
			'updated'          => true,
			'plugin_file'      => $plugin_file,
			'previous_version' => $current_version,
			'installed_version' => $installed_after,
			'active'           => true,
		);
	}

	/**
	 * Reactivate one plugin only when the installed version matches the target
	 * version recorded in the failed Hub update plan.
	 *
	 * @param mixed $input Plugin file and expected installed version.
	 * @return array|\WP_Error
	 */
	public static function execute_activate_plugin( $input = array() ) {
		$plugin_file      = self::get_input_string( $input, 'plugin_file' );
		$expected_version = self::get_input_string( $input, 'expected_installed_version' );
		if ( ! self::is_valid_plugin_file( $plugin_file ) || '' === $expected_version ) {
			return new \WP_Error(
				'kosmos_bridge_invalid_activation_input',
				'Plugin file and expected installed version are required.',
				array( 'status' => 400 )
			);
		}

		if ( ! function_exists( 'get_plugins' ) || ! function_exists( 'is_plugin_active' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}

		$plugins = get_plugins();
		if ( ! isset( $plugins[ $plugin_file ] ) ) {
			return new \WP_Error(
				'kosmos_bridge_plugin_not_installed',
				'The approved plugin is not installed on this site.',
				array( 'status' => 409 )
			);
		}

		$installed_version = isset( $plugins[ $plugin_file ]['Version'] ) ? (string) $plugins[ $plugin_file ]['Version'] : '';
		if ( $installed_version !== $expected_version ) {
			return new \WP_Error(
				'kosmos_bridge_activation_version_mismatch',
				sprintf( 'The approved plugin is at version %s, not the expected version %s.', $installed_version, $expected_version ),
				array( 'status' => 409 )
			);
		}

		$was_active = is_plugin_active( $plugin_file );
		$activation = self::activate_plugin_and_verify( $plugin_file );
		if ( is_wp_error( $activation ) ) {
			return $activation;
		}

		return array(
			'activated'         => ! $was_active,
			'plugin_file'       => $plugin_file,
			'installed_version' => $installed_version,
			'active'            => true,
		);
	}

	/**
	 * Refresh and read WordPress update transients. This intentionally never
	 * downloads or installs updates on the customer site.
	 *
	 * @return array
	 */
	public static function execute_get_available_updates() {
		if ( is_admin() ) {
			return self::collect_available_updates( 'admin' );
		}

		$admin_payload = self::request_admin_update_inventory();
		if ( is_array( $admin_payload ) ) {
			return $admin_payload;
		}

		return self::collect_available_updates( 'standard_fallback' );
	}

	/**
	 * Remember offers contributed through WordPress' shared plugin updater
	 * transient. Vendor updaters often contribute only during normal admin
	 * requests, while a later Hub refresh may only receive WordPress.org data.
	 *
	 * @param mixed $transient Plugin update transient before WordPress stores it.
	 * @return mixed
	 */
	public static function capture_plugin_update_offers( $transient ) {
		$transient_data = self::to_array( $transient );
		$responses      = isset( $transient_data['response'] ) ? self::to_array( $transient_data['response'] ) : array();
		$cached_entries = self::get_cached_plugin_update_entries();
		$now            = time();
		$changed        = false;

		foreach ( $responses as $plugin_file => $offer ) {
			if ( '' === self::get_update_version( self::to_array( $offer ) ) ) {
				continue;
			}

			$cached_entries[ (string) $plugin_file ] = array(
				'captured_at' => $now,
				'offer'       => $offer,
			);
			$changed = true;
		}

		if ( $changed ) {
			update_option( self::UPDATE_OFFER_CACHE_OPTION, $cached_entries, false );
		}

		return $transient;
	}

	/**
	 * Handle the one-time, same-site admin loopback used for update providers
	 * that only register their offers during WordPress admin bootstrap.
	 *
	 * @return void
	 */
	public static function handle_admin_update_loopback() {
		$token = isset( $_POST['token'] ) ? (string) wp_unslash( $_POST['token'] ) : '';
		if ( ! self::consume_loopback_token( $token ) ) {
			wp_send_json_error( array( 'code' => 'kosmos_bridge_loopback_forbidden' ), 403 );
		}

		wp_send_json_success( self::collect_available_updates( 'admin_loopback' ) );
	}

	/**
	 * Read the WordPress update state without installing or downloading updates.
	 *
	 * @param string $check_mode Source context for this update inventory.
	 * @return array
	 */
	private static function collect_available_updates( $check_mode ) {
		global $wp_version;

		if ( ! function_exists( 'get_plugins' ) ) {
			require_once ABSPATH . 'wp-admin/includes/plugin.php';
		}
		if ( ! function_exists( 'get_plugin_updates' ) ) {
			require_once ABSPATH . 'wp-admin/includes/update.php';
		}

		$current_wordpress_version = is_string( $wp_version ) ? $wp_version : get_bloginfo( 'version' );
		$plugins                   = get_plugins();
		self::refresh_update_transients( $plugins );
		$wordpress_updates          = array();
		$plugin_updates             = array();
		$theme_updates              = array();
		$preferred_core_update      = null;
		$site_locale                = function_exists( 'get_locale' ) ? get_locale() : '';

		$core_transient = self::to_array( get_site_transient( 'update_core' ) );
		$core_offers    = isset( $core_transient['updates'] ) ? (array) $core_transient['updates'] : array();
		foreach ( $core_offers as $update ) {
			$data    = self::to_array( $update );
			$version = isset( $data['version'] ) ? (string) $data['version'] : '';

			if ( '' === $version || version_compare( $version, $current_wordpress_version, '<=' ) ) {
				continue;
			}

			$candidate = array(
				'current_version' => $current_wordpress_version,
				'new_version'     => $version,
				'locale'          => isset( $data['locale'] ) ? (string) $data['locale'] : '',
			);

			if ( self::is_preferred_core_update( $candidate, $preferred_core_update, $site_locale ) ) {
				$preferred_core_update = $candidate;
			}
		}

		if ( null !== $preferred_core_update ) {
			$wordpress_updates[] = $preferred_core_update;
		}

		$native_plugin_updates = get_plugin_updates();
		foreach ( $native_plugin_updates as $plugin_file => $plugin ) {
			$plugin_record = self::to_array( $plugin );
			$data          = isset( $plugin_record['update'] ) ? self::to_array( $plugin_record['update'] ) : array();
			$resolved_file = isset( $data['plugin'] ) ? (string) $data['plugin'] : (string) $plugin_file;
			$plugin_data   = isset( $plugins[ $resolved_file ] ) ? $plugins[ $resolved_file ] : $plugin_record;
			$new_version   = self::get_update_version( $data );
			$has_package   = isset( $data['package'] ) && '' !== trim( (string) $data['package'] );
			$is_crocoblock = self::is_crocoblock_plugin_file( $resolved_file );

			$plugin_updates[] = array(
				'plugin_file'     => $resolved_file,
				'name'            => isset( $plugin_data['Name'] ) ? (string) $plugin_data['Name'] : $resolved_file,
				'current_version' => isset( $plugin_data['Version'] ) ? (string) $plugin_data['Version'] : '',
				'new_version'     => $new_version,
				'update_source'   => $is_crocoblock ? 'crocoblock' : 'wordpress',
				'execution_ready' => $has_package,
				'execution_note'  => self::plugin_update_execution_note( $has_package, $is_crocoblock ),
			);
		}

		$plugin_updates = self::append_crocoblock_plugin_updates( $plugin_updates, $plugins );

		$theme_transient = self::to_array( get_site_transient( 'update_themes' ) );
		$theme_responses = isset( $theme_transient['response'] ) ? self::to_array( $theme_transient['response'] ) : array();
		foreach ( $theme_responses as $stylesheet => $update ) {
			$data        = self::to_array( $update );
			$new_version = isset( $data['new_version'] ) ? (string) $data['new_version'] : '';

			if ( '' === $new_version ) {
				continue;
			}

			$theme = wp_get_theme( (string) $stylesheet );
			$theme_updates[] = array(
				'stylesheet'      => (string) $stylesheet,
				'name'            => $theme->exists() ? (string) $theme->get( 'Name' ) : (string) $stylesheet,
				'current_version' => $theme->exists() ? (string) $theme->get( 'Version' ) : '',
				'new_version'     => $new_version,
			);
		}

		return array(
			'reported_at' => gmdate( 'c' ),
			'check_mode'  => $check_mode,
			'wordpress'   => array( 'updates' => $wordpress_updates ),
			'plugins'     => array( 'updates' => $plugin_updates ),
			'themes'      => array( 'updates' => $theme_updates ),
			'summary'     => array(
				'wordpress' => count( $wordpress_updates ),
				'plugins'   => count( $plugin_updates ),
				'themes'    => count( $theme_updates ),
				'total'     => count( $wordpress_updates ) + count( $plugin_updates ) + count( $theme_updates ),
			),
		);
	}

	/**
	 * Crocoblock's Jet Dashboard can provide valid update data without adding
	 * every installed Jet plugin to WordPress' shared update transient. Read the
	 * vendor manager's own list and normalize it into the common inventory.
	 *
	 * @param array $plugin_updates Existing normalized update entries.
	 * @param array $plugins Installed plugin metadata keyed by plugin file.
	 * @return array
	 */
	private static function append_crocoblock_plugin_updates( $plugin_updates, $plugins ) {
		if ( ! class_exists( '\\Jet_Dashboard\\Dashboard' ) ) {
			return $plugin_updates;
		}

		try {
			$dashboard = \Jet_Dashboard\Dashboard::get_instance();
			$manager   = isset( $dashboard->plugin_manager ) ? $dashboard->plugin_manager : null;
			$offers    = is_object( $manager ) && method_exists( $manager, 'get_remote_jet_plugin_list' ) ? $manager->get_remote_jet_plugin_list() : false;
		} catch ( \Throwable $exception ) {
			return $plugin_updates;
		}

		if ( ! is_array( $offers ) ) {
			return $plugin_updates;
		}

		$known_files = array();
		foreach ( $plugin_updates as $update ) {
			if ( is_array( $update ) && isset( $update['plugin_file'] ) ) {
				$known_files[ (string) $update['plugin_file'] ] = true;
			}
		}

		foreach ( $offers as $offer ) {
			$offer_data  = self::to_array( $offer );
			$plugin_file = isset( $offer_data['slug'] ) ? (string) $offer_data['slug'] : '';
			$new_version = isset( $offer_data['version'] ) ? trim( (string) $offer_data['version'] ) : '';
			$plugin_data = isset( $plugins[ $plugin_file ] ) && is_array( $plugins[ $plugin_file ] ) ? $plugins[ $plugin_file ] : array();
			$installed   = isset( $plugin_data['Version'] ) ? (string) $plugin_data['Version'] : '';

			if (
				'' === $plugin_file ||
				'' === $new_version ||
				'' === $installed ||
				isset( $known_files[ $plugin_file ] ) ||
				! self::is_crocoblock_plugin_file( $plugin_file ) ||
				! version_compare( $new_version, $installed, '>' )
			) {
				continue;
			}

			$has_package = self::has_crocoblock_update_package( $plugin_file );
			$plugin_updates[] = array(
				'plugin_file'     => $plugin_file,
				'name'            => isset( $plugin_data['Name'] ) ? (string) $plugin_data['Name'] : $plugin_file,
				'current_version' => $installed,
				'new_version'     => $new_version,
				'update_source'   => 'crocoblock',
				'execution_ready' => $has_package,
				'execution_note'  => self::plugin_update_execution_note( $has_package, true ),
			);
			$known_files[ $plugin_file ] = true;
		}

		return $plugin_updates;
	}

	/**
	 * @param string $plugin_file WordPress plugin file returned by Jet Dashboard.
	 * @return bool
	 */
	private static function is_crocoblock_plugin_file( $plugin_file ) {
		return 0 === strpos( $plugin_file, 'jet-' );
	}

	/**
	 * A version notice is not necessarily an executable update. Crocoblock only
	 * exposes its package after the site has an active provider license.
	 *
	 * @param bool $has_package Whether WordPress currently has an update package.
	 * @param bool $is_crocoblock Whether the update is provided by Crocoblock.
	 * @return string
	 */
	private static function plugin_update_execution_note( $has_package, $is_crocoblock ) {
		if ( $has_package ) {
			return 'An authorized update package is ready.';
		}

		if ( $is_crocoblock ) {
			return 'Crocoblock must activate a valid license for this site before its update package is available.';
		}

		return 'The update provider did not supply an authorized package for this update.';
	}

	/**
	 * Ask the installed Jet Dashboard whether it can provide a package without
	 * returning or persisting the license-bound URL outside WordPress.
	 *
	 * @param string $plugin_file Plugin file relative to WP_PLUGIN_DIR.
	 * @return bool
	 */
	private static function has_crocoblock_update_package( $plugin_file ) {
		if ( ! class_exists( '\\Jet_Dashboard\\Utils' ) ) {
			return false;
		}

		try {
			if ( ! \Jet_Dashboard\Utils::is_site_activated() ) {
				return false;
			}

			return (bool) \Jet_Dashboard\Utils::package_url( $plugin_file );
		} catch ( \Throwable $exception ) {
			return false;
		}
	}

	/**
	 * Provide the read-only core abilities on WordPress versions without the
	 * native Abilities API so older managed sites use the same MCP contract.
	 *
	 * @return array
	 */
	public static function get_fallback_abilities() {
		return array(
			array(
				'name'          => 'kosmos-bridge/get-site-info',
				'label'         => __( 'Get Site Info', 'kosmos-bridge' ),
				'description'   => __( 'Returns basic information about this WordPress site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::updraftplus_backup_start_input_schema(),
				'output_schema' => self::site_info_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/get-environment-info',
				'label'         => __( 'Get Environment Info', 'kosmos-bridge' ),
				'description'   => __( 'Returns software versions and runtime information for this WordPress site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::environment_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/list-active-plugins',
				'label'         => __( 'List Active Plugins', 'kosmos-bridge' ),
				'description'   => __( 'Returns active plugin metadata for this WordPress site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::active_plugins_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/get-available-updates',
				'label'         => __( 'Get Available Updates', 'kosmos-bridge' ),
				'description'   => __( 'Checks and returns currently available WordPress, plugin, and theme updates for this site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::available_updates_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/get-updraftplus-backup-status',
				'label'         => __( 'Get UpdraftPlus Backup Status', 'kosmos-bridge' ),
				'description'   => __( 'Returns read-only metadata about the latest complete UpdraftPlus backup or one requested backup.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::updraftplus_backup_status_input_schema(),
				'output_schema' => self::updraftplus_backup_status_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/start-updraftplus-backup',
				'label'         => __( 'Start UpdraftPlus Backup', 'kosmos-bridge' ),
				'description'   => __( 'Queues one full protected UpdraftPlus backup in the WordPress background. It cannot download, restore, or change backup settings.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::updraftplus_backup_start_output_schema(),
				'meta'          => self::mutation_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/list-updraftplus-backups',
				'label'         => __( 'List UpdraftPlus Backups', 'kosmos-bridge' ),
				'description'   => __( 'Returns read-only metadata for the UpdraftPlus backup sets known to this site.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::updraftplus_backup_start_input_schema(),
				'output_schema' => self::updraftplus_backup_list_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/delete-updraftplus-backup',
				'label'         => __( 'Delete UpdraftPlus Backup', 'kosmos-bridge' ),
				'description'   => __( 'Deletes one exact complete or partial UpdraftPlus backup set locally and from its configured remote storage.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::updraftplus_backup_delete_input_schema(),
				'output_schema' => self::updraftplus_backup_delete_output_schema(),
				'meta'          => self::destructive_mutation_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/verify-updraftplus-backup-deletion',
				'label'         => __( 'Verify UpdraftPlus Backup Deletion', 'kosmos-bridge' ),
				'description'   => __( 'Rescans configured remote storage and confirms whether one exact UpdraftPlus backup set is gone. It does not delete backup files.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::updraftplus_backup_deletion_verification_input_schema(),
				'output_schema' => self::updraftplus_backup_deletion_verification_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/check-site-health',
				'label'         => __( 'Check Site Health', 'kosmos-bridge' ),
				'description'   => __( 'Performs read-only public homepage and WordPress REST API health checks.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => array(),
				'output_schema' => self::site_health_output_schema(),
				'meta'          => self::readonly_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/update-plugin',
				'label'         => __( 'Update Plugin', 'kosmos-bridge' ),
				'description'   => __( 'Updates one active plugin after an exact version and active-state preflight.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::plugin_update_input_schema(),
				'output_schema' => self::plugin_update_output_schema(),
				'meta'          => self::mutation_meta(),
			),
			array(
				'name'          => 'kosmos-bridge/activate-plugin',
				'label'         => __( 'Activate Plugin', 'kosmos-bridge' ),
				'description'   => __( 'Reactivates one plugin only when its installed version matches the Hub recovery plan.', 'kosmos-bridge' ),
				'category'      => 'kosmos-bridge',
				'input_schema'  => self::plugin_activation_input_schema(),
				'output_schema' => self::plugin_activation_output_schema(),
				'meta'          => self::mutation_meta(),
			),
		);
	}

	/**
	 * @param string $ability_name Ability identifier.
	 * @return array|null
	 */
	public static function get_fallback_ability( $ability_name ) {
		foreach ( self::get_fallback_abilities() as $ability ) {
			if ( $ability_name === $ability['name'] ) {
				return $ability;
			}
		}

		return null;
	}

	/**
	 * @param string $ability_name Ability identifier.
	 * @param mixed  $input Ability input.
	 * @return array|null
	 */
	public static function execute_fallback_ability( $ability_name, $input = null ) {
		if ( 'kosmos-bridge/start-updraftplus-backup' === $ability_name ) {
			return self::execute_start_updraftplus_backup();
		}
		if ( 'kosmos-bridge/delete-updraftplus-backup' === $ability_name ) {
			return self::execute_delete_updraftplus_backup( $input );
		}
		if ( 'kosmos-bridge/verify-updraftplus-backup-deletion' === $ability_name ) {
			return self::execute_verify_updraftplus_backup_deletion( $input );
		}
		if ( 'kosmos-bridge/update-plugin' === $ability_name ) {
			return self::execute_update_plugin( $input );
		}
		if ( 'kosmos-bridge/activate-plugin' === $ability_name ) {
			return self::execute_activate_plugin( $input );
		}

		if ( null !== $input && ! empty( $input ) && 'kosmos-bridge/get-updraftplus-backup-status' !== $ability_name ) {
			return null;
		}

		switch ( $ability_name ) {
			case 'kosmos-bridge/get-site-info':
				return self::execute_get_site_info();
			case 'kosmos-bridge/get-environment-info':
				return self::execute_get_environment_info();
			case 'kosmos-bridge/list-active-plugins':
				return self::execute_list_active_plugins();
			case 'kosmos-bridge/get-available-updates':
				return self::execute_get_available_updates();
			case 'kosmos-bridge/get-updraftplus-backup-status':
				return self::execute_get_updraftplus_backup_status( $input );
			case 'kosmos-bridge/list-updraftplus-backups':
				return self::execute_list_updraftplus_backups();
			case 'kosmos-bridge/check-site-health':
				return self::execute_check_site_health();
		}

		return null;
	}

	/**
	 * @param object $ability Ability instance.
	 * @return bool
	 */
	public static function is_public_ability( $ability ) {
		if ( ! is_object( $ability ) || ! method_exists( $ability, 'get_meta_item' ) ) {
			return false;
		}

		return true === $ability->get_meta_item( 'public', false );
	}

	/**
	 * @param object $ability Ability instance.
	 * @return array
	 */
	public static function serialize_ability( $ability ) {
		return array(
			'name'          => $ability->get_name(),
			'label'         => $ability->get_label(),
			'description'   => $ability->get_description(),
			'category'      => $ability->get_category(),
			'input_schema'  => self::normalize_object( $ability->get_input_schema() ),
			'output_schema' => self::normalize_object( $ability->get_output_schema() ),
			'meta'          => self::normalize_object( $ability->get_meta() ),
		);
	}

	/**
	 * @param mixed $value Value returned by the Abilities API.
	 * @return array
	 */
	private static function normalize_object( $value ) {
		return is_array( $value ) ? $value : array();
	}

	/**
	 * @return array
	 */
	private static function readonly_meta() {
		return array(
			'public'       => true,
			'show_in_rest' => true,
			'annotations'  => array(
				'readonly'    => true,
				'destructive' => false,
				'idempotent'  => true,
			),
		);
	}

	/**
	 * @return array
	 */
	private static function mutation_meta() {
		return array(
			'public'       => true,
			'show_in_rest' => true,
			'annotations'  => array(
				'readonly'    => false,
				'destructive' => false,
				'idempotent'  => false,
			),
		);
	}

	/**
	 * @return array
	 */
	private static function destructive_mutation_meta() {
		return array(
			'public'       => true,
			'show_in_rest' => true,
			'annotations'  => array(
				'readonly'    => false,
				'destructive' => true,
				'idempotent'  => false,
			),
		);
	}

	/**
	 * @return array
	 */
	private static function plugin_update_input_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'plugin_file'              => array( 'type' => 'string' ),
				'expected_current_version' => array( 'type' => 'string' ),
				'expected_target_version'  => array( 'type' => 'string' ),
			),
			'required'   => array( 'plugin_file', 'expected_current_version', 'expected_target_version' ),
		);
	}

	/**
	 * @return array
	 */
	private static function plugin_update_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'updated'           => array( 'type' => 'boolean' ),
				'plugin_file'       => array( 'type' => 'string' ),
				'previous_version'  => array( 'type' => 'string' ),
				'installed_version' => array( 'type' => 'string' ),
				'active'            => array( 'type' => 'boolean' ),
			),
			'required'   => array( 'updated', 'plugin_file', 'previous_version', 'installed_version', 'active' ),
		);
	}

	private static function plugin_activation_input_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'plugin_file'                => array( 'type' => 'string' ),
				'expected_installed_version' => array( 'type' => 'string' ),
			),
			'required'   => array( 'plugin_file', 'expected_installed_version' ),
		);
	}

	/**
	 * @return array
	 */
	private static function plugin_activation_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'activated'         => array( 'type' => 'boolean' ),
				'plugin_file'       => array( 'type' => 'string' ),
				'installed_version' => array( 'type' => 'string' ),
				'active'            => array( 'type' => 'boolean' ),
			),
			'required'   => array( 'activated', 'plugin_file', 'installed_version', 'active' ),
		);
	}

	/**
	 * @return array
	 */
	private static function site_health_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'home_url'     => array( 'type' => 'string', 'format' => 'uri' ),
				'rest_url'     => array( 'type' => 'string', 'format' => 'uri' ),
				'home_status'  => array( 'type' => 'integer' ),
				'rest_status'  => array( 'type' => 'integer' ),
				'home_healthy' => array( 'type' => 'boolean' ),
				'rest_healthy' => array( 'type' => 'boolean' ),
				'message'      => array( 'type' => 'string' ),
			),
			'required'   => array( 'home_url', 'rest_url', 'home_status', 'rest_status', 'home_healthy', 'rest_healthy', 'message' ),
		);
	}

	/**
	 * @return array
	 */
	private static function site_info_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'name'         => array( 'type' => 'string' ),
				'description'  => array( 'type' => 'string' ),
				'home_url'     => array( 'type' => 'string', 'format' => 'uri' ),
				'site_url'     => array( 'type' => 'string', 'format' => 'uri' ),
				'language'     => array( 'type' => 'string' ),
				'timezone'     => array( 'type' => 'string' ),
				'is_multisite' => array( 'type' => 'boolean' ),
			),
			'required'   => array( 'name', 'home_url', 'site_url', 'language', 'timezone', 'is_multisite' ),
		);
	}

	/**
	 * @return array
	 */
	private static function environment_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'wordpress_version' => array( 'type' => 'string' ),
				'php_version'       => array( 'type' => 'string' ),
				'bridge_version'    => array( 'type' => 'string' ),
				'abilities_api'     => array( 'type' => 'boolean' ),
				'site_uuid'         => array( 'type' => 'string' ),
			),
			'required'   => array( 'wordpress_version', 'php_version', 'bridge_version', 'abilities_api', 'site_uuid' ),
		);
	}

	/**
	 * @return array
	 */
	private static function active_plugins_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'count'   => array( 'type' => 'integer' ),
				'plugins' => array(
					'type'  => 'array',
					'items' => array(
						'type'       => 'object',
						'properties' => array(
							'plugin_file' => array( 'type' => 'string' ),
							'name'        => array( 'type' => 'string' ),
							'version'     => array( 'type' => 'string' ),
							'author'      => array( 'type' => 'string' ),
							'plugin_uri'  => array( 'type' => 'string' ),
						),
						'required'   => array( 'plugin_file', 'name', 'version', 'author', 'plugin_uri' ),
					),
				),
			),
			'required'   => array( 'count', 'plugins' ),
		);
	}

	/**
	 * @return array
	 */
	private static function available_updates_output_schema() {
		$update_item = array(
			'type'       => 'object',
			'properties' => array(
				'name'            => array( 'type' => 'string' ),
				'current_version' => array( 'type' => 'string' ),
				'new_version'     => array( 'type' => 'string' ),
				'update_source'   => array( 'type' => 'string' ),
				'execution_ready' => array( 'type' => 'boolean' ),
				'execution_note'  => array( 'type' => 'string' ),
			),
			'required'   => array( 'current_version', 'new_version' ),
		);

		return array(
			'type'       => 'object',
			'properties' => array(
				'reported_at' => array( 'type' => 'string', 'format' => 'date-time' ),
				'check_mode'  => array( 'type' => 'string' ),
				'wordpress'   => array( 'type' => 'object' ),
				'plugins'     => array(
					'type'       => 'object',
					'properties' => array( 'updates' => array( 'type' => 'array', 'items' => $update_item ) ),
				),
				'themes'      => array(
					'type'       => 'object',
					'properties' => array( 'updates' => array( 'type' => 'array', 'items' => $update_item ) ),
				),
				'summary'     => array( 'type' => 'object' ),
			),
			'required'   => array( 'reported_at', 'check_mode', 'wordpress', 'plugins', 'themes', 'summary' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_status_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'reported_at'      => array( 'type' => 'string', 'format' => 'date-time' ),
				'provider'         => array( 'type' => 'string' ),
				'installed'        => array( 'type' => 'boolean' ),
				'active'           => array( 'type' => 'boolean' ),
				'available'        => array( 'type' => 'boolean' ),
				'complete'         => array( 'type' => 'boolean' ),
				'retention_protected' => array( 'type' => 'boolean' ),
				// Empty when UpdraftPlus has no complete backup yet; the Hub parses a timestamp only when present.
				'latest_backup_at' => array( 'type' => 'string' ),
				'backup_nonce'     => array( 'type' => 'string' ),
				'backup_count'     => array( 'type' => 'integer' ),
				'components'       => array( 'type' => 'array', 'items' => array( 'type' => 'string' ) ),
				'request_status'   => array( 'type' => 'string' ),
				'request_updated_at' => array( 'type' => 'string' ),
				'request_message'  => array( 'type' => 'string' ),
				'message'          => array( 'type' => 'string' ),
			),
			'required'   => array(
				'reported_at',
				'provider',
				'installed',
				'active',
				'available',
				'complete',
				'retention_protected',
				'latest_backup_at',
				'backup_nonce',
				'backup_count',
				'components',
				'request_status',
				'request_updated_at',
				'request_message',
				'message',
			),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_status_input_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'backup_nonce' => array( 'type' => 'string' ),
			),
		);
	}

	/**
	 * A mutation must explicitly declare its empty object input. WordPress'
	 * Abilities API validates every provided input, including an empty object.
	 *
	 * @return array
	 */
	private static function updraftplus_backup_start_input_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_start_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'accepted'                       => array( 'type' => 'boolean' ),
				'provider'                       => array( 'type' => 'string' ),
				'backup_nonce'                   => array( 'type' => 'string' ),
				'retention_protection_requested' => array( 'type' => 'boolean' ),
				'request_status'                 => array( 'type' => 'string' ),
				'background_dispatch_requested'  => array( 'type' => 'boolean' ),
				'scheduled_at'                   => array( 'type' => 'string', 'format' => 'date-time' ),
				'message'                        => array( 'type' => 'string' ),
			),
			'required'   => array( 'accepted', 'provider', 'backup_nonce', 'retention_protection_requested', 'request_status', 'background_dispatch_requested', 'scheduled_at', 'message' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_list_output_schema() {
		$backup_item = array(
			'type'       => 'object',
			'properties' => array(
				'backup_nonce'        => array( 'type' => 'string' ),
				'backup_timestamp'    => array( 'type' => 'integer' ),
				'backup_at'           => array( 'type' => 'string', 'format' => 'date-time' ),
				'complete'            => array( 'type' => 'boolean' ),
				'retention_protected' => array( 'type' => 'boolean' ),
				'components'          => array( 'type' => 'array', 'items' => array( 'type' => 'string' ) ),
			),
			'required'   => array( 'backup_nonce', 'backup_timestamp', 'backup_at', 'complete', 'retention_protected', 'components' ),
		);

		return array(
			'type'       => 'object',
			'properties' => array(
				'provider'  => array( 'type' => 'string' ),
				'installed' => array( 'type' => 'boolean' ),
				'active'    => array( 'type' => 'boolean' ),
				'backups'   => array( 'type' => 'array', 'items' => $backup_item ),
				'message'   => array( 'type' => 'string' ),
			),
			'required'   => array( 'provider', 'installed', 'active', 'backups', 'message' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_delete_input_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'backup_nonce'           => array( 'type' => 'string' ),
				'backup_timestamp'       => array( 'type' => 'integer' ),
				'delete_remote'          => array( 'type' => 'boolean' ),
				'allow_protected_delete' => array( 'type' => 'boolean' ),
			),
			'required'   => array( 'backup_nonce', 'backup_timestamp', 'delete_remote', 'allow_protected_delete' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_delete_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'backup_nonce'           => array( 'type' => 'string' ),
				'backup_timestamp'       => array( 'type' => 'integer' ),
				'delete_remote'          => array( 'type' => 'boolean' ),
				'status'                 => array( 'type' => 'string' ),
				'completed'              => array( 'type' => 'boolean' ),
				'backup_sets_removed'    => array( 'type' => 'integer' ),
				'local_files_deleted'    => array( 'type' => 'integer' ),
				'remote_files_deleted'   => array( 'type' => 'integer' ),
				'message'                => array( 'type' => 'string' ),
			),
			'required'   => array( 'backup_nonce', 'backup_timestamp', 'delete_remote', 'status', 'completed', 'backup_sets_removed', 'local_files_deleted', 'remote_files_deleted', 'message' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_deletion_verification_input_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'backup_nonce'     => array( 'type' => 'string' ),
				'backup_timestamp' => array( 'type' => 'integer' ),
			),
			'required'   => array( 'backup_nonce', 'backup_timestamp' ),
		);
	}

	/**
	 * @return array
	 */
	private static function updraftplus_backup_deletion_verification_output_schema() {
		return array(
			'type'       => 'object',
			'properties' => array(
				'backup_nonce'         => array( 'type' => 'string' ),
				'backup_timestamp'     => array( 'type' => 'integer' ),
				'verified'             => array( 'type' => 'boolean' ),
				'remaining_components' => array( 'type' => 'array', 'items' => array( 'type' => 'string' ) ),
				'message'              => array( 'type' => 'string' ),
			),
			'required'   => array( 'backup_nonce', 'backup_timestamp', 'verified', 'remaining_components', 'message' ),
		);
	}

	/**
	 * @param array  $history UpdraftPlus backup history.
	 * @param string $nonce Backup nonce selected by UpdraftPlus.
	 * @return array
	 */
	private static function find_updraftplus_backup_by_nonce( $history, $nonce ) {
		if ( '' === $nonce ) {
			return array();
		}

		foreach ( $history as $backup_time => $backup ) {
			if ( ! is_array( $backup ) || ! isset( $backup['nonce'] ) || $nonce !== (string) $backup['nonce'] ) {
				continue;
			}

			$backup['_kosmos_backup_time'] = $backup_time;
			return $backup;
		}

		return array();
	}

	/**
	 * @param mixed $input Ability input.
	 * @return string
	 */
	private static function get_requested_updraftplus_backup_nonce( $input ) {
		if ( ! is_array( $input ) || ! isset( $input['backup_nonce'] ) ) {
			return '';
		}

		$nonce = (string) $input['backup_nonce'];
		return self::is_valid_updraftplus_backup_nonce( $nonce ) ? $nonce : '';
	}

	/**
	 * @param mixed $nonce UpdraftPlus backup nonce.
	 * @return bool
	 */
	private static function is_valid_updraftplus_backup_nonce( $nonce ) {
		return is_string( $nonce ) && 1 === preg_match( '/^[a-f0-9]{12}$/', $nonce );
	}

	/**
	 * @param string $nonce UpdraftPlus backup nonce.
	 * @return void
	 */
	private static function clear_updraftplus_backup_request( $nonce ) {
		$pending = self::get_updraftplus_backup_request( $nonce );
		if ( ! empty( $pending ) ) {
			delete_option( self::UPDRAFT_BACKUP_REQUEST_OPTION );
		}
	}

	/**
	 * @return array
	 */
	private static function get_current_updraftplus_backup_request() {
		$pending = get_option( self::UPDRAFT_BACKUP_REQUEST_OPTION, array() );
		if ( ! is_array( $pending ) || empty( $pending['nonce'] ) || ! self::is_valid_updraftplus_backup_nonce( $pending['nonce'] ) ) {
			return array();
		}

		$updated_at = isset( $pending['updated_at'] ) ? (int) $pending['updated_at'] : 0;
		if ( $updated_at < time() - self::UPDRAFT_BACKUP_REQUEST_TTL ) {
			delete_option( self::UPDRAFT_BACKUP_REQUEST_OPTION );
			return array();
		}

		return $pending;
	}

	/**
	 * @param string $nonce UpdraftPlus backup nonce.
	 * @return array
	 */
	private static function get_updraftplus_backup_request( $nonce ) {
		if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) ) {
			return array();
		}

		$pending = self::get_current_updraftplus_backup_request();
		if ( empty( $pending ) || $nonce !== $pending['nonce'] ) {
			return array();
		}

		return $pending;
	}

	/**
	 * @param string $nonce UpdraftPlus backup nonce.
	 * @param array  $changes Request fields to persist.
	 * @return void
	 */
	private static function update_updraftplus_backup_request( $nonce, $changes ) {
		$pending = self::get_updraftplus_backup_request( $nonce );
		if ( empty( $pending ) ) {
			return;
		}

		self::store_updraftplus_backup_request( array_merge( $pending, $changes, array( 'updated_at' => time() ) ) );
	}

	/**
	 * @param array $pending Current Bridge backup request state.
	 * @return void
	 */
	private static function store_updraftplus_backup_request( $pending ) {
		update_option( self::UPDRAFT_BACKUP_REQUEST_OPTION, $pending, false );
	}

	/**
	 * @param string $nonce UpdraftPlus backup nonce.
	 * @param string $token One-time local loopback token.
	 * @return bool
	 */
	private static function consume_updraftplus_backup_loopback_token( $nonce, $token ) {
		if ( ! self::is_valid_updraftplus_backup_nonce( $nonce ) || '' === $token ) {
			return false;
		}

		$pending  = self::get_updraftplus_backup_request( $nonce );
		$expected = isset( $pending['dispatch_token_hash'] ) ? $pending['dispatch_token_hash'] : '';
		if ( ! is_string( $expected ) || ! hash_equals( $expected, hash( 'sha256', $token ) ) ) {
			return false;
		}

		unset( $pending['dispatch_token_hash'] );
		self::store_updraftplus_backup_request( array_merge( $pending, array( 'updated_at' => time() ) ) );
		return true;
	}

	/**
	 * @param array $backup UpdraftPlus backup history entry.
	 * @return int
	 */
	private static function get_updraftplus_backup_timestamp( $backup ) {
		foreach ( array( '_kosmos_backup_time', 'backup_time', 'nonincremental_backup_time' ) as $key ) {
			if ( isset( $backup[ $key ] ) && is_numeric( $backup[ $key ] ) && (int) $backup[ $key ] > 0 ) {
				return (int) $backup[ $key ];
			}
		}

		return 0;
	}

	/**
	 * @param array $backup UpdraftPlus backup history entry.
	 * @return array
	 */
	private static function get_updraftplus_backup_components( $backup ) {
		$components = array();
		$entities   = array(
			'db'      => 'database',
			'plugins' => 'plugins',
			'themes'  => 'themes',
			'uploads' => 'uploads',
			'others'  => 'others',
		);

		foreach ( $entities as $prefix => $label ) {
			foreach ( array_keys( $backup ) as $backup_key ) {
				if ( 0 === strpos( (string) $backup_key, $prefix ) ) {
					$components[] = $label;
					break;
				}
			}
		}

		return $components;
	}

	/**
	 * @return array{installed: bool, active: bool}
	 */
	private static function get_updraftplus_availability() {
		$plugin_file = 'updraftplus/updraftplus.php';
		$installed   = file_exists( WP_PLUGIN_DIR . '/' . $plugin_file );
		$active      = in_array( $plugin_file, (array) get_option( 'active_plugins', array() ), true );

		if ( is_multisite() ) {
			$network_active = (array) get_site_option( 'active_sitewide_plugins', array() );
			$active         = $active || isset( $network_active[ $plugin_file ] );
		}

		return array(
			'installed' => $installed,
			'active'    => $active,
		);
	}

	/**
	 * Match UpdraftPlus' own definition of a complete backup while keeping
	 * migrated or remote-send-only records out of Kosmos cleanup decisions.
	 *
	 * @param array $backup UpdraftPlus backup history entry.
	 * @return bool
	 */
	private static function is_complete_updraftplus_backup( $backup ) {
		global $updraftplus;

		if ( ! is_array( $backup ) || ! is_object( $updraftplus ) || ! method_exists( $updraftplus, 'get_backupable_file_entities' ) ) {
			return false;
		}

		$remote_sent = ! empty( $backup['service'] ) && (
			( is_array( $backup['service'] ) && in_array( 'remotesend', $backup['service'], true ) ) || 'remotesend' === $backup['service']
		);
		if ( $remote_sent ) {
			return false;
		}

		try {
			$entities = $updraftplus->get_backupable_file_entities( true, true );
		} catch ( \Throwable $exception ) {
			return false;
		}

		if ( ! is_array( $entities ) ) {
			return false;
		}

		foreach ( $entities as $key => $entity ) {
			if ( ! \UpdraftPlus_Options::get_updraft_option( 'updraft_include_' . $key, false ) ) {
				continue;
			}
			if ( ! isset( $backup[ $key ] ) ) {
				return false;
			}
		}

		return true;
	}

	/**
	 * @param mixed $history_time History array key.
	 * @param array $backup UpdraftPlus backup history entry.
	 * @return int
	 */
	private static function get_updraftplus_history_timestamp( $history_time, $backup ) {
		if ( is_numeric( $history_time ) && (int) $history_time > 0 ) {
			return (int) $history_time;
		}

		return self::get_updraftplus_backup_timestamp( $backup );
	}

	/**
	 * @return object|null
	 */
	private static function get_updraftplus_admin() {
		if ( ! class_exists( 'UpdraftPlus_Admin', false ) ) {
			$admin_file = WP_PLUGIN_DIR . '/updraftplus/admin.php';
			if ( ! is_readable( $admin_file ) ) {
				return null;
			}

			require_once $admin_file;
		}

		global $updraftplus_admin;
		return isset( $updraftplus_admin ) && is_object( $updraftplus_admin ) ? $updraftplus_admin : null;
	}

	/**
	 * Convert an uninterrupted UpdraftPlus delete result into a stable Hub
	 * contract. A continuation is treated as incomplete rather than retried
	 * with partially consumed provider state.
	 *
	 * @param string $nonce Backup nonce.
	 * @param int    $timestamp UpdraftPlus backup history timestamp.
	 * @param mixed  $deletion Provider response.
	 * @return array
	 */
	private static function updraftplus_backup_delete_result( $nonce, $timestamp, $deletion ) {
		$deletion = is_array( $deletion ) ? $deletion : array();
		$outcome  = isset( $deletion['result'] ) ? (string) $deletion['result'] : 'error';
		$deleted_timestamps = isset( $deletion['deleted_timestamps'] ) ? explode( ',', (string) $deletion['deleted_timestamps'] ) : array();
		$completed = 'success' === $outcome && in_array( (string) $timestamp, $deleted_timestamps, true );
		$status    = $completed ? 'completed' : 'failed';
		$message   = '';

		if ( 'completed' === $status ) {
			$message = 'UpdraftPlus deleted the requested backup locally and from the configured remote storage.';
		} elseif ( 'continue' === $outcome ) {
			$message = 'UpdraftPlus did not complete deletion in one uninterrupted provider operation.';
		} else {
			$message = 'UpdraftPlus did not confirm complete deletion of the requested backup set.';
		}

		return array(
			'backup_nonce'           => $nonce,
			'backup_timestamp'       => $timestamp,
			'delete_remote'          => true,
			'status'                 => $status,
			'completed'              => $completed,
			'backup_sets_removed'    => isset( $deletion['backup_sets'] ) ? max( 0, (int) $deletion['backup_sets'] ) : 0,
			'local_files_deleted'    => isset( $deletion['backup_local'] ) ? max( 0, (int) $deletion['backup_local'] ) : 0,
			'remote_files_deleted'   => isset( $deletion['backup_remote'] ) ? max( 0, (int) $deletion['backup_remote'] ) : 0,
			'message'                => $message,
		);
	}

	/**
	 * @param mixed $input Ability input.
	 * @return int
	 */
	private static function get_updraftplus_backup_timestamp_input( $input ) {
		if ( ! is_array( $input ) || ! isset( $input['backup_timestamp'] ) ) {
			return 0;
		}

		$value = $input['backup_timestamp'];
		if ( is_int( $value ) && $value > 0 ) {
			return $value;
		}

		return is_string( $value ) && ctype_digit( $value ) && (int) $value > 0 ? (int) $value : 0;
	}

	/**
	 * @param mixed  $input Ability input.
	 * @param string $key Input key.
	 * @return bool
	 */
	private static function get_input_bool( $input, $key ) {
		return is_array( $input ) && isset( $input[ $key ] ) && true === $input[ $key ];
	}

	/**
	 * WordPress reports historical and language fallback packages alongside the
	 * recommended core offer. The fleet only needs the one best next upgrade.
	 *
	 * @param array      $candidate Candidate core update.
	 * @param array|null $current Current preferred core update.
	 * @param string     $site_locale Site locale.
	 * @return bool
	 */
	private static function is_preferred_core_update( $candidate, $current, $site_locale ) {
		if ( null === $current ) {
			return true;
		}

		$candidate_is_local = '' !== $site_locale && $candidate['locale'] === $site_locale;
		$current_is_local   = '' !== $site_locale && $current['locale'] === $site_locale;

		if ( $candidate_is_local !== $current_is_local ) {
			return $candidate_is_local;
		}

		return version_compare( $candidate['new_version'], $current['new_version'], '>' );
	}

	/**
	 * WordPress.org uses new_version, while some trusted third-party updaters
	 * use version for the same update offer.
	 *
	 * @param array $data Update response data.
	 * @return string
	 */
	private static function get_update_version( $data ) {
		foreach ( array( 'new_version', 'version' ) as $key ) {
			$version = isset( $data[ $key ] ) ? trim( (string) $data[ $key ] ) : '';
			if ( '' !== $version ) {
				return $version;
			}
		}

		return '';
	}

	/**
	 * @param string $url Public site URL owned by this WordPress installation.
	 * @return array{status:int,healthy:bool}
	 */
	private static function request_health_url( $url ) {
		$response = wp_remote_get(
			$url,
			array(
				'timeout'             => 15,
				'redirection'         => 3,
				'sslverify'           => true,
				'limit_response_size' => 1024,
				'user-agent'          => 'Kosmos Bridge/' . \KosmosBridge\Options::get_bridge_version(),
			)
		);

		if ( is_wp_error( $response ) ) {
			return array(
				'status'  => 0,
				'healthy' => false,
			);
		}

		$status = (int) wp_remote_retrieve_response_code( $response );
		return array(
			'status'  => $status,
			'healthy' => $status >= 200 && $status < 400,
		);
	}

	/**
	 * @param array{status:int,healthy:bool} $home Homepage response result.
	 * @param array{status:int,healthy:bool} $rest REST API response result.
	 * @return string
	 */
	private static function site_health_message( $home, $rest ) {
		if ( $home['healthy'] && $rest['healthy'] ) {
			return 'Public homepage and WordPress REST API health checks passed.';
		}

		$failed = array();
		if ( ! $home['healthy'] ) {
			$failed[] = 'public homepage';
		}
		if ( ! $rest['healthy'] ) {
			$failed[] = 'WordPress REST API';
		}

		return implode( ' and ', $failed ) . ' health check did not return a successful HTTP response.';
	}

	/**
	 * Run the same inventory inside admin-ajax so admin-only update providers
	 * can attach their standard WordPress update offers.
	 *
	 * @return array|null
	 */
	private static function request_admin_update_inventory() {
		$token = self::generate_loopback_token();
		set_transient( self::loopback_token_key( $token ), hash( 'sha256', $token ), self::UPDATE_LOOPBACK_TTL );

		$response = wp_remote_post(
			admin_url( 'admin-ajax.php' ),
			array(
				'timeout'     => 30,
				'redirection' => 0,
				'body'        => array(
					'action' => self::UPDATE_LOOPBACK_ACTION,
					'token'  => $token,
				),
			)
		);
		delete_transient( self::loopback_token_key( $token ) );

		if ( is_wp_error( $response ) || 200 !== (int) wp_remote_retrieve_response_code( $response ) ) {
			return null;
		}

		$payload = json_decode( wp_remote_retrieve_body( $response ), true );
		if ( ! is_array( $payload ) || empty( $payload['success'] ) || ! isset( $payload['data'] ) || ! is_array( $payload['data'] ) ) {
			return null;
		}

		return $payload['data'];
	}

	/**
	 * @param string $token One-time loopback token.
	 * @return bool
	 */
	private static function consume_loopback_token( $token ) {
		if ( '' === $token ) {
			return false;
		}

		$key      = self::loopback_token_key( $token );
		$expected = get_transient( $key );
		delete_transient( $key );

		return is_string( $expected ) && hash_equals( $expected, hash( 'sha256', $token ) );
	}

	/**
	 * @return string
	 */
	private static function generate_loopback_token() {
		try {
			return bin2hex( random_bytes( 32 ) );
		} catch ( \Exception $exception ) {
			return wp_generate_password( 64, false, false );
		}
	}

	/**
	 * @param string $token One-time loopback token.
	 * @return string
	 */
	private static function loopback_token_key( $token ) {
		return 'kosmos_bridge_update_loopback_' . hash( 'sha256', $token );
	}

	/**
	 * Refresh official update sources without discarding offers that trusted
	 * third-party updaters already registered in the current WordPress cache.
	 *
	 * @param array $plugins Installed plugin metadata keyed by plugin file.
	 * @return void
	 */
	private static function refresh_update_transients( $plugins ) {
		if ( ! function_exists( 'wp_version_check' ) ) {
			require_once ABSPATH . WPINC . '/update.php';
		}

		$previous_plugin_updates = self::to_array( get_site_transient( 'update_plugins' ) );
		$previous_theme_updates  = self::to_array( get_site_transient( 'update_themes' ) );

		delete_site_transient( 'update_core' );
		self::mark_update_transient_stale( 'update_plugins' );
		self::mark_update_transient_stale( 'update_themes' );
		wp_version_check();
		wp_update_plugins();
		wp_update_themes();

		self::restore_missing_plugin_update_responses( $previous_plugin_updates, $plugins );
		self::restore_missing_plugin_update_responses(
			array( 'response' => self::get_cached_plugin_update_offers() ),
			$plugins,
			true
		);
		self::restore_missing_theme_update_responses( $previous_theme_updates );
	}

	/**
	 * Ask WordPress to perform a fresh standard update check without deleting
	 * third-party update offers that are already present in its shared cache.
	 *
	 * @param string $transient_name Update transient name.
	 * @return void
	 */
	private static function mark_update_transient_stale( $transient_name ) {
		$transient = get_site_transient( $transient_name );

		if ( is_object( $transient ) ) {
			$transient->last_checked = 0;
			set_site_transient( $transient_name, $transient );
			return;
		}

		if ( is_array( $transient ) ) {
			$transient['last_checked'] = 0;
			set_site_transient( $transient_name, $transient );
		}
	}

	/**
	 * @return array
	 */
	private static function get_cached_plugin_update_offers() {
		$offers = array();

		foreach ( self::get_cached_plugin_update_entries() as $plugin_file => $entry ) {
			if ( isset( $entry['offer'] ) ) {
				$offers[ $plugin_file ] = $entry['offer'];
			}
		}

		return $offers;
	}

	/**
	 * @return array
	 */
	private static function get_cached_plugin_update_entries() {
		$stored = get_option( self::UPDATE_OFFER_CACHE_OPTION, array() );
		$stored = is_array( $stored ) ? $stored : array();
		$now    = time();
		$valid  = array();

		foreach ( $stored as $plugin_file => $entry ) {
			if ( ! is_array( $entry ) ) {
				continue;
			}

			$captured_at = isset( $entry['captured_at'] ) ? (int) $entry['captured_at'] : 0;
			if ( $captured_at < ( $now - self::UPDATE_OFFER_CACHE_TTL ) || ! isset( $entry['offer'] ) ) {
				continue;
			}

			$valid[ (string) $plugin_file ] = $entry;
		}

		return $valid;
	}

	/**
	 * Keep valid offers from premium and vendor updaters when a core refresh did
	 * not put them back into the shared update transient.
	 *
	 * @param array $previous_transient Update transient before the refresh.
	 * @param array $plugins Installed plugin metadata keyed by plugin file.
	 * @param bool  $prefer_cached_offer Whether a valid vendor cache may take
	 *                                   precedence over WordPress.org no-update data.
	 * @return void
	 */
	private static function restore_missing_plugin_update_responses( $previous_transient, $plugins, $prefer_cached_offer = false ) {
		$previous_responses = isset( $previous_transient['response'] ) ? self::to_array( $previous_transient['response'] ) : array();
		$current            = get_site_transient( 'update_plugins' );
		$current_data       = self::to_array( $current );
		$current_responses  = isset( $current_data['response'] ) ? self::to_array( $current_data['response'] ) : array();
		$current_no_update  = isset( $current_data['no_update'] ) ? self::to_array( $current_data['no_update'] ) : array();
		$changed            = false;

		if ( empty( $previous_responses ) || empty( $current_data ) ) {
			return;
		}

		foreach ( $previous_responses as $plugin_file => $update ) {
			$update_data   = self::to_array( $update );
			$resolved_file = isset( $update_data['plugin'] ) ? (string) $update_data['plugin'] : (string) $plugin_file;
			$plugin_data   = isset( $plugins[ $resolved_file ] ) && is_array( $plugins[ $resolved_file ] ) ? $plugins[ $resolved_file ] : array();
			$new_version   = self::get_update_version( $update_data );
			$installed     = isset( $plugin_data['Version'] ) ? (string) $plugin_data['Version'] : '';

			if (
				'' === $resolved_file ||
				'' === $new_version ||
				'' === $installed ||
				! version_compare( $new_version, $installed, '>' ) ||
				array_key_exists( $resolved_file, $current_responses ) ||
				( ! $prefer_cached_offer && array_key_exists( $resolved_file, $current_no_update ) )
			) {
				continue;
			}

			$current_responses[ $resolved_file ] = $update;
			$changed                              = true;
		}

		if ( $changed ) {
			self::store_update_responses( 'update_plugins', $current, $current_data, $current_responses );
		}
	}

	/**
	 * Apply the same protection to premium themes that expose their offers
	 * through WordPress' standard update transient.
	 *
	 * @param array $previous_transient Update transient before the refresh.
	 * @return void
	 */
	private static function restore_missing_theme_update_responses( $previous_transient ) {
		$previous_responses = isset( $previous_transient['response'] ) ? self::to_array( $previous_transient['response'] ) : array();
		$current            = get_site_transient( 'update_themes' );
		$current_data       = self::to_array( $current );
		$current_responses  = isset( $current_data['response'] ) ? self::to_array( $current_data['response'] ) : array();
		$current_no_update  = isset( $current_data['no_update'] ) ? self::to_array( $current_data['no_update'] ) : array();
		$changed            = false;

		if ( empty( $previous_responses ) || empty( $current_data ) || ! function_exists( 'wp_get_theme' ) ) {
			return;
		}

		foreach ( $previous_responses as $stylesheet => $update ) {
			$update_data = self::to_array( $update );
			$new_version = isset( $update_data['new_version'] ) ? (string) $update_data['new_version'] : '';
			$theme       = wp_get_theme( (string) $stylesheet );
			$installed   = $theme->exists() ? (string) $theme->get( 'Version' ) : '';

			if (
				'' === $new_version ||
				'' === $installed ||
				! version_compare( $new_version, $installed, '>' ) ||
				array_key_exists( $stylesheet, $current_responses ) ||
				array_key_exists( $stylesheet, $current_no_update )
			) {
				continue;
			}

			$current_responses[ $stylesheet ] = $update;
			$changed                           = true;
		}

		if ( $changed ) {
			self::store_update_responses( 'update_themes', $current, $current_data, $current_responses );
		}
	}

	/**
	 * @param string $transient_name Update transient name.
	 * @param mixed  $current Raw transient value.
	 * @param array  $current_data Normalized transient value.
	 * @param array  $responses Merged update responses.
	 * @return void
	 */
	private static function store_update_responses( $transient_name, $current, $current_data, $responses ) {
		if ( is_object( $current ) ) {
			$current->response = $responses;
		} else {
			$current             = $current_data;
			$current['response'] = $responses;
		}

		set_site_transient( $transient_name, $current );
	}

	/**
	 * @return void
	 */
	private static function load_plugin_upgrader() {
		require_once ABSPATH . 'wp-admin/includes/file.php';
		require_once ABSPATH . 'wp-admin/includes/misc.php';
		require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader.php';
		require_once ABSPATH . 'wp-admin/includes/class-wp-upgrader-skins.php';
		require_once ABSPATH . 'wp-admin/includes/class-plugin-upgrader.php';
	}

	/**
	 * Plugin_Upgrader may temporarily deactivate an active plugin during an
	 * update. Restore and verify the expected active state before reporting the
	 * mutation as successful to the Hub.
	 *
	 * @param string $plugin_file Plugin file relative to WP_PLUGIN_DIR.
	 * @return true|\WP_Error
	 */
	private static function activate_plugin_and_verify( $plugin_file ) {
		if ( is_plugin_active( $plugin_file ) ) {
			return true;
		}

		$activation = activate_plugin( $plugin_file );
		if ( is_wp_error( $activation ) ) {
			return $activation;
		}

		if ( ! is_plugin_active( $plugin_file ) ) {
			return new \WP_Error(
				'kosmos_bridge_activation_verification_failed',
				'WordPress did not confirm that the approved plugin is active.',
				array( 'status' => 500 )
			);
		}

		return true;
	}

	/**
	 * @param string $plugin_file Plugin file relative to WP_PLUGIN_DIR.
	 * @return bool
	 */
	private static function is_valid_plugin_file( $plugin_file ) {
		return 1 === preg_match( '/^(?:[A-Za-z0-9][A-Za-z0-9._-]*\/)*[A-Za-z0-9][A-Za-z0-9._-]*\.php$/', $plugin_file );
	}

	/**
	 * @param mixed  $input Ability input.
	 * @param string $key Input field name.
	 * @return string
	 */
	private static function get_input_string( $input, $key ) {
		if ( ! is_array( $input ) || ! isset( $input[ $key ] ) ) {
			return '';
		}

		return trim( (string) $input[ $key ] );
	}

	/**
	 * Normalize WordPress transients, which can be arrays or stdClass values.
	 *
	 * @param mixed $value Value to normalize.
	 * @return array
	 */
	private static function to_array( $value ) {
		if ( is_array( $value ) ) {
			return $value;
		}

		return is_object( $value ) ? get_object_vars( $value ) : array();
	}
}
