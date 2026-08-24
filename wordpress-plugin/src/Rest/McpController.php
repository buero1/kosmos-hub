<?php
namespace KosmosBridge\Rest;

use KosmosBridge\Abilities\Registry;
use KosmosBridge\Security\SiteAuth;
use WP_Error;
use WP_REST_Server;

defined( 'ABSPATH' ) || exit;

class McpController {
	/**
	 * @return void
	 */
	public static function register_routes() {
		register_rest_route(
			'kosmos-bridge/v1',
			'/mcp/discover-abilities',
			array(
				'methods'             => WP_REST_Server::CREATABLE,
				'permission_callback' => array( SiteAuth::class, 'authorize_request' ),
				'callback'            => array( self::class, 'discover_abilities' ),
			)
		);

		register_rest_route(
			'kosmos-bridge/v1',
			'/mcp/get-ability-info',
			array(
				'methods'             => WP_REST_Server::CREATABLE,
				'permission_callback' => array( SiteAuth::class, 'authorize_request' ),
				'callback'            => array( self::class, 'get_ability_info' ),
			)
		);

		register_rest_route(
			'kosmos-bridge/v1',
			'/mcp/execute-ability',
			array(
				'methods'             => WP_REST_Server::CREATABLE,
				'permission_callback' => array( SiteAuth::class, 'authorize_request' ),
				'callback'            => array( self::class, 'execute_ability' ),
			)
		);
	}

	/**
	 * @param \WP_REST_Request $request REST request.
	 * @return array|WP_Error
	 */
	public static function discover_abilities( $request ) {
		if ( ! function_exists( 'wp_get_abilities' ) ) {
			return self::missing_abilities_api_error();
		}

		$params        = self::get_json_params( $request );
		$ability_names = isset( $params['ability_names'] ) && is_array( $params['ability_names'] ) ? $params['ability_names'] : array();
		$abilities     = array();

		foreach ( wp_get_abilities() as $ability ) {
			if ( ! Registry::is_public_ability( $ability ) ) {
				continue;
			}

			if ( ! empty( $ability_names ) && ! in_array( $ability->get_name(), $ability_names, true ) ) {
				continue;
			}

			$abilities[] = Registry::serialize_ability( $ability );
		}

		return array(
			'server'    => 'kosmos-bridge',
			'abilities' => $abilities,
		);
	}

	/**
	 * @param \WP_REST_Request $request REST request.
	 * @return array|WP_Error
	 */
	public static function get_ability_info( $request ) {
		if ( ! function_exists( 'wp_get_ability' ) ) {
			return self::missing_abilities_api_error();
		}

		$params       = self::get_json_params( $request );
		$ability_name = isset( $params['ability_name'] ) ? (string) $params['ability_name'] : '';
		if ( '' === $ability_name ) {
			return self::invalid_request_error( 'ability_name is required.' );
		}

		$ability = wp_get_ability( $ability_name );
		if ( ! $ability || ! Registry::is_public_ability( $ability ) ) {
			return new WP_Error(
				'kosmos_bridge_ability_not_found',
				sprintf( 'Ability "%s" is not available.', $ability_name ),
				array( 'status' => 404 )
			);
		}

		return array(
			'ability' => Registry::serialize_ability( $ability ),
		);
	}

	/**
	 * @param \WP_REST_Request $request REST request.
	 * @return array|WP_Error
	 */
	public static function execute_ability( $request ) {
		if ( ! function_exists( 'wp_get_ability' ) ) {
			return self::missing_abilities_api_error();
		}

		$params       = self::get_json_params( $request );
		$ability_name = isset( $params['ability_name'] ) ? (string) $params['ability_name'] : '';
		$input        = isset( $params['input'] ) ? $params['input'] : null;
		if ( '' === $ability_name ) {
			return self::invalid_request_error( 'ability_name is required.' );
		}

		$ability = wp_get_ability( $ability_name );
		if ( ! $ability || ! Registry::is_public_ability( $ability ) ) {
			return new WP_Error(
				'kosmos_bridge_ability_not_found',
				sprintf( 'Ability "%s" is not available.', $ability_name ),
				array( 'status' => 404 )
			);
		}

		$result = null === $input ? $ability->execute() : $ability->execute( $input );
		if ( is_wp_error( $result ) ) {
			return $result;
		}

		return array(
			'ability_name' => $ability_name,
			'result'       => $result,
		);
	}

	/**
	 * @param \WP_REST_Request $request REST request.
	 * @return array
	 */
	private static function get_json_params( $request ) {
		$params = $request->get_json_params();
		return is_array( $params ) ? $params : array();
	}

	/**
	 * @return WP_Error
	 */
	private static function missing_abilities_api_error() {
		return new WP_Error(
			'kosmos_bridge_abilities_api_missing',
			'The WordPress Abilities API is not available on this site.',
			array( 'status' => 501 )
		);
	}

	/**
	 * @param string $message Error message.
	 * @return WP_Error
	 */
	private static function invalid_request_error( $message ) {
		return new WP_Error(
			'kosmos_bridge_invalid_request',
			$message,
			array( 'status' => 400 )
		);
	}
}
