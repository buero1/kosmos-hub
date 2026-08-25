<?php
namespace KosmosBridge\Updates;

use KosmosBridge\Options;

defined( 'ABSPATH' ) || exit;

/**
 * Supplies WordPress with trusted update metadata from the Kosmos endpoint.
 */
class PluginUpdater {
	const PLUGIN_SLUG        = 'kosmos-bridge';
	const UPDATE_HOST        = 'plugins.kosmos-medien.de';
	const DEFAULT_HOMEPAGE   = 'https://kosmos-hub.31-70-92-95.sslip.io';
	const METADATA_URL       = 'https://plugins.kosmos-medien.de/kosmos-bridge/metadata.json';
	const METADATA_CACHE_KEY = 'kosmos_bridge_update_metadata_v1';
	const METADATA_CACHE_TTL = 900;

	/**
	 * @var array<string, mixed>|null
	 */
	private $metadata = null;

	/**
	 * @return void
	 */
	public static function boot() {
		$instance = new self();

		// Native WordPress provider hook for the plugin header's Update URI.
		add_filter( 'update_plugins_' . self::UPDATE_HOST, array( $instance, 'provide_wordpress_update' ), 10, 4 );
		add_filter( 'pre_set_site_transient_update_plugins', array( $instance, 'inject_update' ) );
		add_filter( 'site_transient_update_plugins', array( $instance, 'inject_stored_update' ) );
		add_filter( 'plugins_api', array( $instance, 'get_plugin_information' ), 20, 3 );
	}

	/**
	 * @param mixed $transient Plugin update transient.
	 * @return mixed
	 */
	public function inject_update( $transient ) {
		return $this->apply_update_metadata( $transient, true );
	}

	/**
	 * WordPress reads this transient again immediately before an update runs.
	 * Reinstate the trusted response there so the update screen and upgrader
	 * cannot disagree about whether a package is available.
	 *
	 * @param mixed $transient Plugin update transient.
	 * @return mixed
	 */
	public function inject_stored_update( $transient ) {
		return $this->apply_update_metadata( $transient, false, true );
	}

	/**
	 * Provide the update data through WordPress' dedicated Update URI hook.
	 * This is used whenever WordPress rebuilds the plugin update transient.
	 *
	 * @param mixed  $update Existing provider response.
	 * @param array  $plugin_data Plugin header data.
	 * @param string $plugin_file Plugin basename.
	 * @param array  $locales Installed locales.
	 * @return mixed
	 */
	public function provide_wordpress_update( $update, $plugin_data, $plugin_file, $locales ) {
		$expected_plugin_file = plugin_basename( KOSMOS_BRIDGE_PLUGIN_FILE );
		if ( $expected_plugin_file !== (string) $plugin_file ) {
			return $update;
		}

		$metadata        = $this->get_metadata( true );
		$current_version = isset( $plugin_data['Version'] ) ? (string) $plugin_data['Version'] : Options::get_bridge_version();

		if ( empty( $metadata ) || version_compare( (string) $metadata['version'], $current_version, '<=' ) ) {
			return false;
		}

		return $this->build_update_offer( $metadata, $expected_plugin_file );
	}

	/**
	 * @param mixed $transient Plugin update transient.
	 * @param bool  $force_metadata_refresh Whether to bypass the short metadata cache.
	 * @param bool  $bootstrap_missing_state Whether to rebuild the minimum state
	 *                                       needed by WordPress' updater.
	 * @return mixed
	 */
	private function apply_update_metadata( $transient, $force_metadata_refresh, $bootstrap_missing_state = false ) {
		if ( ! is_object( $transient ) ) {
			if ( ! $bootstrap_missing_state ) {
				return $transient;
			}

			$transient = new \stdClass();
		}

		$plugin_file = plugin_basename( KOSMOS_BRIDGE_PLUGIN_FILE );

		if ( ! isset( $transient->checked ) || ! is_array( $transient->checked ) ) {
			if ( ! $bootstrap_missing_state ) {
				return $transient;
			}

			$transient->checked = array();
		}

		if ( ! array_key_exists( $plugin_file, $transient->checked ) ) {
			if ( ! $bootstrap_missing_state ) {
				return $transient;
			}

			// The update screen can clear this transient just before the upgrader
			// reads it. The loaded Bridge version is sufficient for this one offer.
			$transient->checked[ $plugin_file ] = Options::get_bridge_version();
		}

		if ( empty( $transient->checked ) ) {
			return $transient;
		}

		$metadata = $this->get_metadata( $force_metadata_refresh );

		if ( empty( $metadata ) || version_compare( (string) $metadata['version'], Options::get_bridge_version(), '<=' ) ) {
			$this->remove_stale_update( $transient, $plugin_file );
			return $transient;
		}

		if ( ! isset( $transient->response ) || ! is_array( $transient->response ) ) {
			$transient->response = array();
		}

		$transient->response[ $plugin_file ] = $this->build_update_offer( $metadata, $plugin_file );

		return $transient;
	}

	/**
	 * @param array<string, mixed> $metadata Validated update metadata.
	 * @param string               $plugin_file Plugin basename.
	 * @return object
	 */
	private function build_update_offer( array $metadata, $plugin_file ) {
		return (object) array(
			'id'           => self::METADATA_URL,
			'slug'         => self::PLUGIN_SLUG,
			'plugin'       => $plugin_file,
			'version'      => (string) $metadata['version'],
			'new_version'  => (string) $metadata['version'],
			'url'          => (string) $metadata['homepage'],
			'package'      => (string) $metadata['download_url'],
			'tested'       => (string) $metadata['tested'],
			'requires'     => (string) $metadata['requires'],
			'requires_php' => (string) $metadata['requires_php'],
		);
	}

	/**
	 * @param object $transient Plugin update transient.
	 * @param string $plugin_file Plugin basename.
	 * @return void
	 */
	private function remove_stale_update( $transient, $plugin_file ) {
		if ( isset( $transient->response ) && is_array( $transient->response ) ) {
			unset( $transient->response[ $plugin_file ] );
		}
	}

	/**
	 * @param mixed  $result Existing WordPress API result.
	 * @param string $action Requested API action.
	 * @param mixed  $args   Requested API arguments.
	 * @return mixed
	 */
	public function get_plugin_information( $result, $action, $args ) {
		if ( 'plugin_information' !== (string) $action || ! is_object( $args ) || self::PLUGIN_SLUG !== (string) ( $args->slug ?? '' ) ) {
			return $result;
		}

		$metadata = $this->get_metadata();

		if ( empty( $metadata ) ) {
			return $result;
		}

		return (object) array(
			'name'          => (string) $metadata['name'],
			'slug'          => self::PLUGIN_SLUG,
			'version'       => (string) $metadata['version'],
			'author'        => 'Kosmos Medien',
			'homepage'      => (string) $metadata['homepage'],
			'requires'      => (string) $metadata['requires'],
			'tested'        => (string) $metadata['tested'],
			'requires_php'  => (string) $metadata['requires_php'],
			'last_updated'  => (string) $metadata['last_updated'],
			'download_link' => (string) $metadata['download_url'],
			// WordPress 6.2 writes sanitized sections back by array key.
			// Keeping this as an array avoids a fatal error in its details dialog.
			'sections'      => $metadata['sections'],
		);
	}

	/**
	 * @return array<string, mixed>|null
	 */
	private function get_metadata( $force_refresh = false ) {
		if ( ! $force_refresh && null !== $this->metadata ) {
			return $this->metadata;
		}

		if ( ! $force_refresh ) {
			$cached_metadata = get_site_transient( self::METADATA_CACHE_KEY );
			if ( is_array( $cached_metadata ) ) {
				$this->metadata = $cached_metadata;
				return $this->metadata;
			}
		}

		$response = wp_remote_get(
			self::METADATA_URL,
			array(
				'timeout'     => 10,
				'redirection' => 3,
				'headers'     => array(
					'Accept' => 'application/json',
				),
			)
		);

		if ( is_wp_error( $response ) || 200 !== (int) wp_remote_retrieve_response_code( $response ) ) {
			return null;
		}

		$metadata = json_decode( wp_remote_retrieve_body( $response ), true );
		$metadata = is_array( $metadata ) ? $metadata : array();

		$this->metadata = $this->sanitize_metadata( $metadata );
		if ( null !== $this->metadata ) {
			set_site_transient( self::METADATA_CACHE_KEY, $this->metadata, self::METADATA_CACHE_TTL );
		}

		return $this->metadata;
	}

	/**
	 * @param array<string, mixed> $metadata Untrusted response data.
	 * @return array<string, mixed>|null
	 */
	private function sanitize_metadata( array $metadata ) {
		$version      = isset( $metadata['version'] ) ? trim( (string) $metadata['version'] ) : '';
		$download_url = isset( $metadata['download_url'] ) ? esc_url_raw( (string) $metadata['download_url'] ) : '';
		$homepage     = isset( $metadata['homepage'] ) ? esc_url_raw( (string) $metadata['homepage'] ) : self::DEFAULT_HOMEPAGE;

		if ( ! preg_match( '/^\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?$/', $version ) ) {
			return null;
		}

		if ( 0 !== strpos( $download_url, 'https://plugins.kosmos-medien.de/kosmos-bridge/' ) || 0 !== strpos( $homepage, 'https://' ) ) {
			return null;
		}

		$sections = isset( $metadata['sections'] ) && is_array( $metadata['sections'] ) ? $metadata['sections'] : array();

		return array(
			'name'         => sanitize_text_field( $metadata['name'] ?? 'Kosmos Bridge' ),
			'version'      => $version,
			'download_url' => $download_url,
			'homepage'     => $homepage,
			'requires'     => sanitize_text_field( $metadata['requires'] ?? '6.9' ),
			'tested'       => sanitize_text_field( $metadata['tested'] ?? '' ),
			'requires_php' => sanitize_text_field( $metadata['requires_php'] ?? '7.4' ),
			'last_updated' => sanitize_text_field( $metadata['last_updated'] ?? '' ),
			'sections'     => array(
				'description' => wp_kses_post( $sections['description'] ?? '' ),
				'changelog'   => wp_kses_post( $sections['changelog'] ?? '' ),
			),
		);
	}
}
